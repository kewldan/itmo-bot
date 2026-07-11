"""Бэкап БД: ежедневный gzip-JSON дамп админу в Telegram + CLI восстановления.

Формат: {"users": [...], "programs": [...], "snapshots": [...],
"subscriptions": [...]}; datetime сериализуются в ISO-строки.
Восстановление: `python -m app.backup restore <файл>` — вставка
с ON CONFLICT DO NOTHING, существующие данные не перезаписываются.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
from aiogram.exceptions import TelegramAPIError

from app.admin import send_admin_document
from app.config import MSK, settings
from app.db import Database

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger(__name__)

_SELECTS = {
    "users": "SELECT * FROM users ORDER BY tg_id",
    "programs": "SELECT * FROM programs ORDER BY id",
    "snapshots": "SELECT * FROM snapshots ORDER BY id",
    "subscriptions": "SELECT * FROM subscriptions ORDER BY id",
}

_MIN_SLEEP_SECONDS = 60.0


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


async def export_dump(db: Database) -> bytes:
    """Собирает все таблицы в gzip-JSON."""
    payload: dict[str, list[dict[str, object]]] = {}
    for table, query in _SELECTS.items():
        rows = await db.pool.fetch(query)
        payload[table] = [
            {key: _jsonable(value) for key, value in dict(row).items()} for row in rows
        ]
    raw = json.dumps(payload, ensure_ascii=False).encode()
    return gzip.compress(raw)


def _dt(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


async def restore_dump(db: Database, data: bytes) -> dict[str, int]:
    """Восстанавливает дамп; возвращает число вставленных строк по таблицам."""
    payload = json.loads(gzip.decompress(data))
    counts = dict.fromkeys(_SELECTS, 0)
    for row in payload.get("users", []):
        status = await db.pool.execute(
            """
            INSERT INTO users (tg_id, created_at) VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            row["tg_id"],
            _dt(row.get("created_at")),
        )
        counts["users"] += int(status.endswith("1"))
    for row in payload.get("programs", []):
        status = await db.pool.execute(
            """
            INSERT INTO programs
                (id, degree, financing, group_id, title, places, last_update_time)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            """,
            row["id"],
            row["degree"],
            row["financing"],
            row["group_id"],
            row["title"],
            row["places"],
            _dt(row.get("last_update_time")),
        )
        counts["programs"] += int(status.endswith("1"))
    for row in payload.get("snapshots", []):
        status = await db.pool.execute(
            """
            INSERT INTO snapshots
                (program_id, update_time, fetched_at, total, agreements,
                 approved, paid, places, items)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (program_id, update_time) DO NOTHING
            """,
            row["program_id"],
            _dt(row.get("update_time")),
            _dt(row.get("fetched_at")),
            row["total"],
            row["agreements"],
            row["approved"],
            row["paid"],
            row["places"],
            row["items"],
        )
        counts["snapshots"] += int(status.endswith("1"))
    for row in payload.get("subscriptions", []):
        status = await db.pool.execute(
            """
            INSERT INTO subscriptions
                (tg_id, program_id, sspvo_id, notify, place_interval_hours,
                 place_notified_at, last_p_base, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (tg_id, program_id, sspvo_id) DO NOTHING
            """,
            row["tg_id"],
            row["program_id"],
            row["sspvo_id"],
            row["notify"],
            row.get("place_interval_hours", 6),
            _dt(row.get("place_notified_at")),
            row.get("last_p_base"),
            _dt(row.get("created_at")),
        )
        counts["subscriptions"] += int(status.endswith("1"))
    return counts


def _seconds_until_backup(now: datetime) -> float:
    """Секунды до ближайшего BACKUP_HOUR:00 по Москве."""
    target = now.astimezone(MSK).replace(
        hour=settings.backup_hour, minute=0, second=0, microsecond=0
    )
    if target <= now.astimezone(MSK):
        target += timedelta(days=1)
    return max((target - now.astimezone(MSK)).total_seconds(), _MIN_SLEEP_SECONDS)


async def backup_loop(bot: Bot, db: Database) -> None:
    """Раз в сутки шлёт дамп БД админу документом."""
    if settings.admin_tg_id is None:
        log.info("ADMIN_TG_ID не задан — бэкапы в Telegram выключены")
        return
    while True:
        await asyncio.sleep(_seconds_until_backup(datetime.now(tz=MSK)))
        try:
            data = await export_dump(db)
            stamp = datetime.now(tz=MSK).strftime("%Y-%m-%d")
            payload = json.loads(gzip.decompress(data))
            caption = "💾 Бэкап БД: " + ", ".join(
                f"{table} {len(rows)}" for table, rows in payload.items()
            )
            await send_admin_document(
                bot, data, f"itmo-bot-backup-{stamp}.json.gz", caption
            )
            log.info("Бэкап отправлен админу (%d байт)", len(data))
        except (asyncpg.PostgresError, TelegramAPIError, OSError):
            log.exception("Сбой бэкапа")


async def _run_restore(data: bytes) -> None:
    db = Database(settings.database_url)
    await db.connect()
    try:
        counts = await restore_dump(db, data)
        for table, count in counts.items():
            log.info("%s: вставлено %d", table, count)
    finally:
        await db.close()


def main() -> None:
    """CLI восстановления: python -m app.backup restore <файл>."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Восстановление бэкапа БД")
    parser.add_argument("command", choices=["restore"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    asyncio.run(_run_restore(args.file.read_bytes()))


if __name__ == "__main__":
    main()
