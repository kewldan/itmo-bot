"""Команды: /start /help /status /chart /list /refresh и колбэки /list."""

from __future__ import annotations

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
from app.charts import render_history
from app.fetcher import FetchError, fetch_rating
from app.service import build_analysis, build_history_points, day_delta

if TYPE_CHECKING:
    from app.db import Database
    from app.models import Program, Subscription

router = Router()

TITLE_BUTTON_LIMIT = 40
DIGEST_INTERVALS = (0, 3, 6, 12, 24)  # часы; 0 = выключен


def _interval_label(hours: int) -> str:
    return "Выкл" if hours == 0 else f"{hours} ч"


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    """Приветствие и регистрация пользователя."""
    await db.ensure_user(message.chat.id)
    await message.answer(texts.START, disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка."""
    await message.answer(texts.HELP, disable_web_page_preview=True)


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
        analysis = build_analysis(snaps, program, sub.sspvo_id)
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
        points = build_history_points(snaps, program, sub.sspvo_id)
        png = render_history(points, program.places, program.title)
        note = "" if len(snaps) > 1 else texts.CHART_SINGLE_NOTE
        await message.answer_photo(
            BufferedInputFile(png, filename="dynamics.png"),
            caption=f"{program.title}{note}",
        )
        await wait.delete()


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
