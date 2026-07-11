"""FSM-диалог оформления подписки: ссылка на список -> код ЕПГУ."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import texts
from app.fetcher import FetchError, fetch_rating, parse_program_ref

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext

    from app.db import Database
    from app.fetcher import RatingData

router = Router()

_ID_RE = re.compile(r"^\d{4,12}$")


class TrackForm(StatesGroup):
    """Шаги диалога /track."""

    url = State()
    code = State()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Сбрасывает текущий диалог."""
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(Command("track"))
async def cmd_track(message: Message, state: FSMContext) -> None:
    """Запускает диалог подписки."""
    await state.set_state(TrackForm.url)
    await message.answer(texts.TRACK_ASK_URL)


@router.message(TrackForm.url, F.text)
async def track_url(message: Message, state: FSMContext) -> None:
    """Принимает ссылку на список и проверяет её загрузкой."""
    ref = parse_program_ref(message.text or "")
    if ref is None:
        await message.answer(texts.TRACK_BAD_URL)
        return
    degree, financing, group_id = ref
    await message.answer("Проверяю список…")
    try:
        rating = await fetch_rating(degree, financing, group_id)
    except FetchError as exc:
        await message.answer(f"⚠️ {exc}\nПришлите другую ссылку или /cancel.")
        return
    await state.update_data(degree=degree, financing=financing, group_id=group_id)
    await state.set_state(TrackForm.code)
    await message.answer(
        texts.TRACK_ASK_ID.format(
            title=rating.title, total=rating.total, places=rating.places
        )
    )


async def _fetch_from_state(state: FSMContext) -> RatingData:
    data = await state.get_data()
    return await fetch_rating(
        str(data["degree"]), str(data["financing"]), int(data["group_id"])
    )


@router.message(TrackForm.code, F.text)
async def track_code(message: Message, state: FSMContext, db: Database) -> None:
    """Принимает код ЕПГУ, ищет его в списке и сохраняет подписку."""
    code = (message.text or "").strip()
    if _ID_RE.match(code) is None:
        await message.answer(texts.TRACK_BAD_ID)
        return
    try:
        rating = await _fetch_from_state(state)
    except FetchError as exc:
        await message.answer(f"⚠️ {exc}\nПопробуйте позже: /track")
        await state.clear()
        return

    me = next((i for i in rating.items if i["id"] == code), None)
    if me is None:
        await state.update_data(code=code)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Сохранить всё равно", callback_data="track:force"
                    ),
                    InlineKeyboardButton(text="Отмена", callback_data="track:cancel"),
                ]
            ]
        )
        await message.answer(
            texts.TRACK_NOT_FOUND.format(code=code, total=rating.total),
            reply_markup=keyboard,
        )
        return

    found_line = (
        f"Нашёл вас в списке: место <b>{me['pos']}</b>, балл <b>{me['ts']:g}</b>."
        if me["ts"] is not None
        else f"Нашёл вас в списке: место <b>{me['pos']}</b>."
    )
    await state.clear()
    await _save_subscription(message, db, code, rating, found_line)


@router.callback_query(TrackForm.code, F.data == "track:cancel")
async def track_force_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    """Отмена сохранения не найденного в списке кода."""
    await state.clear()
    if isinstance(cb.message, Message):
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(texts.CANCELLED)
    await cb.answer()


@router.callback_query(TrackForm.code, F.data == "track:force")
async def track_force_save(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    """Сохраняет подписку, хотя код пока не найден в списке."""
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    data = await state.get_data()
    try:
        rating = await _fetch_from_state(state)
    except FetchError as exc:
        await cb.message.answer(f"⚠️ {exc}\nПопробуйте позже: /track")
        await state.clear()
        await cb.answer()
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await _save_subscription(
        cb.message, db, str(data["code"]), rating, texts.TRACK_SAVED_NOT_FOUND
    )
    await cb.answer()


async def _save_subscription(
    message: Message,
    db: Database,
    code: str,
    rating: RatingData,
    found_line: str,
) -> None:
    """Создаёт пользователя, программу, снапшот и подписку."""
    tg_id = message.chat.id
    await db.ensure_user(tg_id)
    program = await db.upsert_program(rating)
    await db.insert_snapshot(program.id, rating)
    await db.add_subscription(tg_id, program.id, code)
    await message.answer(
        texts.TRACK_DONE.format(title=rating.title, code=code, found_line=found_line)
    )
