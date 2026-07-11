"""Фоновый опрос сайта и рассылка уведомлений при обновлении списков."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app import texts
from app.admin import notify_admin
from app.config import settings
from app.fetcher import FetchError, fetch_rating
from app.metrics import diff_snapshots
from app.service import build_analysis, fetch_cross, fetch_lk, window_delta

if TYPE_CHECKING:
    from datetime import datetime

    from aiogram import Bot

    from app.db import Database
    from app.fetcher import RatingData
    from app.models import Program, Snapshot, Subscription

log = logging.getLogger(__name__)

POLITE_PAUSE_SECONDS = 2.0
# Пороги алертов: уведомляем при падении p_base ниже любого из них.
ALERT_THRESHOLDS = (0.95, 0.90, 0.75, 0.50)
# Алерт админу после стольких сбоев обновления программы подряд.
FAIL_ALERT_AFTER = 3
_fail_counts: dict[int, int] = {}


def crossed_threshold(prev: float | None, cur: float) -> float | None:
    """Наибольший порог, через который вероятность упала вниз."""
    if prev is None:
        return None
    for threshold in ALERT_THRESHOLDS:
        if prev >= threshold > cur:
            return threshold
    return None


async def poll_once(bot: Bot, db: Database) -> None:
    """Один проход: обновить все программы с подписками."""
    programs = await db.subscribed_programs()
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        for program in programs:
            try:
                rating = await fetch_rating(
                    program.degree, program.financing, program.group_id, client
                )
            except (FetchError, httpx.HTTPError) as exc:
                log.warning("Не удалось обновить %s: %s", program.url, exc)
                await _register_failure(bot, program, exc)
                continue
            await _register_success(bot, program)
            await _process_update(bot, db, rating)
            await asyncio.sleep(POLITE_PAUSE_SECONDS)


async def _register_failure(bot: Bot, program: Program, exc: Exception) -> None:
    """Считает сбои подряд; после FAIL_ALERT_AFTER — алерт админу."""
    _fail_counts[program.id] = _fail_counts.get(program.id, 0) + 1
    if _fail_counts[program.id] == FAIL_ALERT_AFTER:
        await notify_admin(
            bot,
            f"⚠️ Программа «{program.title}» не обновляется "
            f"({FAIL_ALERT_AFTER} сбоя подряд): {exc}\n{program.url}",
        )


async def _register_success(bot: Bot, program: Program) -> None:
    """Сбрасывает счётчик сбоев; сообщает о восстановлении."""
    if _fail_counts.get(program.id, 0) >= FAIL_ALERT_AFTER:
        await notify_admin(bot, f"✅ Программа «{program.title}» снова обновляется.")
    _fail_counts[program.id] = 0


async def _process_update(bot: Bot, db: Database, rating: RatingData) -> None:
    """Сохраняет снапшот и уведомляет подписчиков, если список изменился."""
    program = await db.upsert_program(rating)
    old_snaps = await db.snapshots(program.id)
    if not await db.insert_snapshot(program.id, rating) or not old_snaps:
        return
    snaps = await db.snapshots(program.id)
    subs = await db.program_subscriptions(program.id)
    for sub in subs:
        await _notify_subscriber(bot, db, program, (old_snaps, snaps), sub)


def _digest_due(sub: Subscription, now: datetime) -> bool:
    """Пора ли слать дайджест места этой подписке."""
    if sub.place_interval_hours <= 0:
        return False
    if sub.place_notified_at is None:
        return True
    elapsed_h = (now - sub.place_notified_at).total_seconds() / 3600.0
    return elapsed_h >= sub.place_interval_hours


async def _notify_subscriber(
    bot: Bot,
    db: Database,
    program: Program,
    history_pair: tuple[list[Snapshot], list[Snapshot]],
    sub: Subscription,
) -> None:
    """Алерт о падении вероятности, дайджест места или уведомление."""
    old_snaps, snaps = history_pair
    prev, latest = old_snaps[-1], snaps[-1]
    lk = await fetch_lk(db, program.id, latest.update_time)
    cross = await fetch_cross(db, program, latest.update_time)
    analysis = build_analysis(snaps, program, sub.sspvo_id, lk, cross)

    if analysis.found and program.places > 0:
        threshold = crossed_threshold(sub.last_p_base, analysis.p_base)
        await db.update_last_p(sub.id, analysis.p_base)
        if threshold is not None and sub.notify:
            await _send(
                bot,
                db,
                sub,
                texts.format_threshold_alert(program.title, threshold, analysis),
            )

    if analysis.found and _digest_due(sub, latest.update_time):
        window = window_delta(snaps, sub.sspvo_id, float(sub.place_interval_hours))
        text = texts.format_place_digest(
            program.title, analysis, window, sub.place_interval_hours
        )
        await _send(bot, db, sub, text)
        await db.mark_place_notified(sub.id, latest.update_time)
        return  # дайджест включает всё — второе сообщение не нужно

    if not sub.notify:
        return
    delta = diff_snapshots(
        (prev.update_time, prev.items),
        (latest.update_time, latest.items),
        sub.sspvo_id,
    )
    if not (delta.d_total or delta.d_paid or delta.d_approved or delta.d_position):
        return  # для этого пользователя ничего не изменилось
    prev_analysis = build_analysis(old_snaps, program, sub.sspvo_id, lk, cross)
    text = texts.format_notification(
        program.title,
        analysis,
        delta,
        prev_analysis.p_base if prev_analysis.found else None,
    )
    await _send(bot, db, sub, text)


async def _send(bot: Bot, db: Database, sub: Subscription, text: str) -> None:
    try:
        await bot.send_message(sub.tg_id, text)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        try:
            await bot.send_message(sub.tg_id, text)
        except TelegramAPIError:
            log.warning("Повторная отправка пользователю %s не удалась", sub.tg_id)
    except TelegramForbiddenError:
        # пользователь заблокировал бота — выключаем уведомления
        await db.set_notify(sub.id, notify=False)
        log.info("Пользователь %s заблокировал бота, уведомления выключены", sub.tg_id)
    except TelegramAPIError:
        log.exception("Не удалось отправить уведомление %s", sub.tg_id)


async def poll_loop(bot: Bot, db: Database) -> None:
    """Бесконечный цикл опроса; переживает любые сбои итерации."""
    log.info("Поллер запущен, интервал %s с", settings.poll_interval_seconds)
    while True:
        results = await asyncio.gather(poll_once(bot, db), return_exceptions=True)
        error = results[0]
        if isinstance(error, BaseException):
            log.error("Сбой цикла опроса", exc_info=error)
        await asyncio.sleep(settings.poll_interval_seconds)
