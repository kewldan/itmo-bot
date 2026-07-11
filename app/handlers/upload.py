"""Загрузка CSV-выгрузки из личного кабинета абитуриента.

Выгрузка ЛК содержит «Дату выбора конкурсной группы» каждой заявки —
настоящую кривую притока, которой нет в публичном списке. Файл
привязывается к программе по коду ЕПГУ одной из подписок пользователя
и используется моделью, пока свежий (48 часов).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import F, Router

from app import texts
from app.config import MSK
from app.metrics import LkPoints, influx_from_lk

if TYPE_CHECKING:
    from aiogram.types import Message

    from app.db import Database
    from app.models import Program, Subscription

log = logging.getLogger(__name__)

router = Router()

MAX_CSV_BYTES = 10 * 1024 * 1024
COL_CODE = "Код поступающего"
COL_TIME = "Дата выбора конкурсной группы по Москве"
COL_SCORE = "Сумма баллов"
_TIME_FORMAT = "%d.%m.%Y в %H:%M"


def _parse_rows(text: str) -> list[tuple[str, datetime, float | None]]:
    """(код, время подачи, балл) из CSV; битые строки пропускаются."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if reader.fieldnames is None or not {COL_CODE, COL_TIME} <= set(reader.fieldnames):
        return []
    rows: list[tuple[str, datetime, float | None]] = []
    for raw in reader:
        try:
            stamp = datetime.strptime(
                (raw.get(COL_TIME) or "").strip(), _TIME_FORMAT
            ).replace(tzinfo=MSK)
        except ValueError:
            continue
        code = (raw.get(COL_CODE) or "").strip()
        if not code:
            continue
        score_text = (raw.get(COL_SCORE) or "").strip()
        try:
            score = float(score_text) if score_text else None
        except ValueError:
            score = None
        rows.append((code, stamp, score))
    return rows


def _match_program(
    subs: list[tuple[Subscription, Program]], codes: set[str]
) -> tuple[Subscription, Program] | None:
    return next(((s, p) for s, p in subs if s.sspvo_id in codes), None)


@router.message(F.document)
async def handle_document(message: Message, db: Database) -> None:
    """Принимает CSV из ЛК и сохраняет кривую притока."""
    document = message.document
    if document is None or message.bot is None:
        return
    name = (document.file_name or "").lower()
    if not name.endswith(".csv"):
        await message.answer(texts.LK_NOT_CSV)
        return
    if document.file_size and document.file_size > MAX_CSV_BYTES:
        await message.answer(texts.LK_TOO_BIG)
        return

    buffer = io.BytesIO()
    await message.bot.download(document, destination=buffer)
    rows = _parse_rows(buffer.getvalue().decode("utf-8-sig", errors="replace"))
    if not rows:
        await message.answer(texts.LK_BAD_FORMAT)
        return

    subs = await db.user_subscriptions(message.chat.id)
    matched = _match_program(subs, {code for code, _, _ in rows})
    if matched is None:
        await message.answer(texts.LK_NO_MATCH.format(rows=len(rows)))
        return
    sub, program = matched

    points: list[list[object]] = [
        [stamp.isoformat(), score] for _, stamp, score in rows
    ]
    await db.upsert_lk_points(program.id, message.chat.id, points)

    lk = LkPoints(
        uploaded_at=datetime.now(tz=MSK),
        points=[(stamp, score) for _, stamp, score in rows],
    )
    me_score = next((score for code, _, score in rows if code == sub.sspvo_id), None)
    influx = influx_from_lk(lk, me_score)
    await message.answer(
        texts.LK_ACCEPTED.format(
            rows=len(rows),
            title=program.title,
            rate=influx.rate_per_day,
            q_ahead=influx.q_ahead * 100,
        )
    )
    log.info(
        "ЛК-выгрузка: %d строк для программы %s от %s",
        len(rows),
        program.id,
        message.chat.id,
    )
