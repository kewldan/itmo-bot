"""Команды: /start /help /status /chart /list /settings /refresh и колбэки."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import texts
from app.admin import notify_admin
from app.charts import render_history
from app.config import MSK
from app.fetcher import FetchError, fetch_rating
from app.service import (
    build_analysis,
    build_history_points,
    day_delta,
    fetch_cross,
    fetch_lk,
)

if TYPE_CHECKING:
    from app.db import Database
    from app.models import Program, Subscription

log = logging.getLogger(__name__)

# Кэш PNG-графиков: (program_id, sspvo_id, update_time) -> bytes.
_CHART_CACHE: dict[tuple[int, str, str], bytes] = {}
_CHART_CACHE_LIMIT = 64

router = Router()

TITLE_BUTTON_LIMIT = 40
DIGEST_INTERVALS = (0, 3, 6, 12, 24)  # часы; 0 = выключен


def _interval_label(hours: int) -> str:
    return "Выкл" if hours == 0 else f"{hours} ч"


async def _notify_admin_new_user(message: Message, db: Database) -> None:
    """Сообщает администратору о новом пользователе (если настроено)."""
    if message.bot is None:
        return
    user = message.from_user
    name = user.full_name if user else "?"
    username = f", @{user.username}" if user and user.username else ""
    text = texts.ADMIN_NEW_USER.format(
        name=name,
        tg_id=message.chat.id,
        username=username,
        total=await db.count_users(),
    )
    await notify_admin(message.bot, text)


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    """Приветствие и регистрация пользователя."""
    created = await db.ensure_user(message.chat.id)
    if created:
        await _notify_admin_new_user(message, db)
    await message.answer(texts.START, disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка."""
    await message.answer(texts.HELP, disable_web_page_preview=True)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Показывает Telegram ID (для настройки ADMIN_TG_ID)."""
    await message.answer(texts.YOUR_ID.format(tg_id=message.chat.id))


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    """О боте: неофициальный, создатель, исходники."""
    await message.answer(texts.ABOUT, disable_web_page_preview=True)


@router.message(Command("status"))
async def cmd_status(message: Message, db: Database) -> None:
    """Детальный разбор по каждой подписке."""
    subs = await db.user_subscriptions(message.chat.id)
    if not subs:
        await message.answer(texts.NO_SUBS)
        return
    for sub, program in subs:
        snaps = await db.snapshots(program.id)
        if not snaps:
            await message.answer(texts.NO_DATA.format(title=program.title))
            continue
        now = datetime.now(tz=MSK)
        lk = await fetch_lk(db, program.id, now)
        cross = await fetch_cross(db, program, now)
        analysis = build_analysis(snaps, program, sub.sspvo_id, lk, cross)
        delta = day_delta(snaps, sub.sspvo_id)
        await message.answer(
            texts.format_status(program.title, program.url, analysis, delta),
            disable_web_page_preview=True,
        )


@router.message(Command("chart"))
async def cmd_chart(message: Message, db: Database) -> None:
    """Графики динамики по каждой подписке."""
    subs = await db.user_subscriptions(message.chat.id)
    if not subs:
        await message.answer(texts.NO_SUBS)
        return
    for sub, program in subs:
        snaps = await db.snapshots(program.id)
        if not snaps:
            await message.answer(texts.NO_DATA.format(title=program.title))
            continue
        wait = await message.answer(texts.CHART_WAIT)
        key = (program.id, sub.sspvo_id, snaps[-1].update_time.isoformat())
        png = _CHART_CACHE.get(key)
        if png is None:
            lk = await fetch_lk(db, program.id, datetime.now(tz=MSK))
            points = build_history_points(snaps, program, sub.sspvo_id, lk)
            png = render_history(points, program.places, program.title)
            if len(_CHART_CACHE) >= _CHART_CACHE_LIMIT:
                _CHART_CACHE.pop(next(iter(_CHART_CACHE)))
            _CHART_CACHE[key] = png
        note = "" if len(snaps) > 1 else texts.CHART_SINGLE_NOTE
        await message.answer_photo(
            BufferedInputFile(png, filename="dynamics.png"),
            caption=f"{program.title}{note}",
        )
        await wait.delete()


@router.message(Command("compare"))
async def cmd_compare(message: Message, db: Database) -> None:
    """Сводная таблица по всем подпискам."""
    subs = await db.user_subscriptions(message.chat.id)
    if not subs:
        await message.answer(texts.NO_SUBS)
        return
    rows = []
    for sub, program in subs:
        snaps = await db.snapshots(program.id)
        if not snaps:
            continue
        now = datetime.now(tz=MSK)
        lk = await fetch_lk(db, program.id, now)
        cross = await fetch_cross(db, program, now)
        rows.append(
            (program.title, build_analysis(snaps, program, sub.sspvo_id, lk, cross))
        )
    if not rows:
        await message.answer(texts.NO_SUBS)
        return
    await message.answer(texts.format_compare(rows))


@router.message(Command("refresh"))
async def cmd_refresh(message: Message, db: Database) -> None:
    """Принудительное обновление всех программ пользователя."""
    subs = await db.user_subscriptions(message.chat.id)
    if not subs:
        await message.answer(texts.NO_SUBS)
        return
    seen: set[int] = set()
    new_count = 0
    for _, program in subs:
        if program.id in seen:
            continue
        seen.add(program.id)
        try:
            rating = await fetch_rating(
                program.degree, program.financing, program.group_id
            )
        except FetchError as exc:
            await message.answer(f"⚠️ {program.title}: {exc}")
            continue
        stored = await db.upsert_program(rating)
        if await db.insert_snapshot(stored.id, rating):
            new_count += 1
    if new_count:
        await message.answer(texts.REFRESH_NEW.format(count=new_count))
    else:
        await message.answer(texts.REFRESH_NONE)


def _subs_keyboard(
    subs: list[tuple[Subscription, Program]],
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{'🔔' if sub.notify else '🔕'} "
                    f"{program.title[:TITLE_BUTTON_LIMIT]} · {sub.sspvo_id}"
                ),
                callback_data=f"sub:toggle:{sub.id}",
            ),
            InlineKeyboardButton(text="❌", callback_data=f"sub:del:{sub.id}"),
        ]
        for sub, program in subs
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("list"))
async def cmd_list(message: Message, db: Database) -> None:
    """Список подписок с кнопками управления."""
    subs = await db.user_subscriptions(message.chat.id)
    if not subs:
        await message.answer(texts.NO_SUBS)
        return
    await message.answer(texts.LIST_HEADER, reply_markup=_subs_keyboard(subs))


def _settings_keyboard(sub: Subscription) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("✅ " if sub.place_interval_hours == hours else "")
            + _interval_label(hours),
            callback_data=f"ivl:{sub.id}:{hours}",
        )
        for hours in DIGEST_INTERVALS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database) -> None:
    """Настройка периодического дайджеста места по каждой подписке."""
    subs = await db.user_subscriptions(message.chat.id)
    if not subs:
        await message.answer(texts.NO_SUBS)
        return
    for sub, program in subs:
        await message.answer(
            texts.SETTINGS_ITEM.format(title=program.title, code=sub.sspvo_id),
            reply_markup=_settings_keyboard(sub),
        )


@router.callback_query(F.data.startswith("ivl:"))
async def cb_interval(cb: CallbackQuery, db: Database) -> None:
    """Смена интервала дайджеста места."""
    parts = (cb.data or "").split(":")
    match parts:
        case ["ivl", sub_id_str, hours_str] if hours_str.isdigit():
            sub_id, hours = int(sub_id_str), int(hours_str)
        case _:
            await cb.answer()
            return
    if hours not in DIGEST_INTERVALS:
        await cb.answer()
        return
    if not await db.set_place_interval(sub_id, cb.from_user.id, hours):
        await cb.answer(texts.SETTINGS_NOT_FOUND)
        return
    label = texts.SETTINGS_OFF_LABEL if hours == 0 else f"раз в {hours} ч"
    await cb.answer(texts.SETTINGS_SAVED.format(label=label))
    if isinstance(cb.message, Message):
        subs = await db.user_subscriptions(cb.from_user.id)
        current = next((s for s, _ in subs if s.id == sub_id), None)
        if current is not None:
            await cb.message.edit_reply_markup(reply_markup=_settings_keyboard(current))


@router.callback_query(F.data.startswith("sub:"))
async def cb_sub(cb: CallbackQuery, db: Database) -> None:
    """Переключение уведомлений и удаление подписки."""
    parts = (cb.data or "").split(":")
    match parts:
        case ["sub", "toggle", sub_id]:
            state = await db.toggle_notify(int(sub_id), cb.from_user.id)
            if state is None:
                await cb.answer("Подписка не найдена")
            else:
                await cb.answer(
                    "Уведомления включены" if state else "Уведомления выключены"
                )
        case ["sub", "del", sub_id]:
            await db.delete_subscription(int(sub_id), cb.from_user.id)
            await cb.answer("Подписка удалена")
        case _:
            await cb.answer()
            return
    if not isinstance(cb.message, Message):
        return
    subs = await db.user_subscriptions(cb.from_user.id)
    if subs:
        await cb.message.edit_reply_markup(reply_markup=_subs_keyboard(subs))
    else:
        await cb.message.edit_text(texts.NO_SUBS)
