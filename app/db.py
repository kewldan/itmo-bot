"""Слой БД: пул asyncpg, схема и все запросы приложения."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import asyncpg

from app.models import CrossList, Program, Snapshot, Subscription

if TYPE_CHECKING:
    from datetime import datetime

    from app.fetcher import CompactItem, RatingData

_SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id SERIAL PRIMARY KEY,
    degree TEXT NOT NULL,
    financing TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    places INTEGER NOT NULL DEFAULT 0,
    last_update_time TIMESTAMPTZ,
    UNIQUE (degree, financing, group_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id BIGSERIAL PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs (id) ON DELETE CASCADE,
    update_time TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    total INTEGER NOT NULL,
    agreements INTEGER NOT NULL,
    approved INTEGER NOT NULL,
    paid INTEGER NOT NULL,
    places INTEGER NOT NULL,
    items JSONB NOT NULL,
    UNIQUE (program_id, update_time)
);

CREATE TABLE IF NOT EXISTS users (
    tg_id BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL REFERENCES users (tg_id) ON DELETE CASCADE,
    program_id INTEGER NOT NULL REFERENCES programs (id) ON DELETE CASCADE,
    sspvo_id TEXT NOT NULL,
    notify BOOLEAN NOT NULL DEFAULT TRUE,
    place_interval_hours INTEGER NOT NULL DEFAULT 6,
    place_notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tg_id, program_id, sspvo_id)
);

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS place_interval_hours INTEGER NOT NULL DEFAULT 6;
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS place_notified_at TIMESTAMPTZ;
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS last_p_base DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS lk_data (
    program_id INTEGER PRIMARY KEY REFERENCES programs (id) ON DELETE CASCADE,
    tg_id BIGINT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    points JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS cross_lists (
    degree TEXT NOT NULL,
    financing TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    places INTEGER NOT NULL,
    update_time TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    items JSONB NOT NULL,
    PRIMARY KEY (degree, financing, group_id)
);
"""


class DatabaseNotConnectedError(RuntimeError):
    """Обращение к БД до вызова Database.connect()."""

    def __init__(self) -> None:
        """Сообщение фиксировано."""
        super().__init__("База данных не подключена: вызовите Database.connect()")


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Кодеки json/jsonb: в Python-объекты и обратно."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


def _program_from(record: asyncpg.Record) -> Program:
    return Program(
        id=record["id"],
        degree=record["degree"],
        financing=record["financing"],
        group_id=record["group_id"],
        title=record["title"],
        places=record["places"],
        last_update_time=record["last_update_time"],
    )


def _subscription_from(record: asyncpg.Record, *, id_key: str = "id") -> Subscription:
    return Subscription(
        id=record[id_key],
        tg_id=record["tg_id"],
        program_id=record["program_id"],
        sspvo_id=record["sspvo_id"],
        notify=record["notify"],
        place_interval_hours=record["place_interval_hours"],
        place_notified_at=record["place_notified_at"],
        last_p_base=record["last_p_base"],
    )


def _snapshot_from(record: asyncpg.Record) -> Snapshot:
    return Snapshot(
        id=record["id"],
        program_id=record["program_id"],
        update_time=record["update_time"],
        fetched_at=record["fetched_at"],
        total=record["total"],
        agreements=record["agreements"],
        approved=record["approved"],
        paid=record["paid"],
        places=record["places"],
        items=cast("list[CompactItem]", record["items"]),
    )


class Database:
    """Все операции приложения с PostgreSQL поверх пула asyncpg."""

    def __init__(self, dsn: str) -> None:
        """Пул создаётся отдельно, в connect()."""
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Создаёт пул соединений и применяет схему."""
        self._pool = await asyncpg.create_pool(
            self._dsn, init=_init_connection, min_size=1, max_size=5
        )
        await self.pool.execute(_SCHEMA)

    async def close(self) -> None:
        """Закрывает пул."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        """Пул соединений; ошибка, если connect() не вызывался."""
        if self._pool is None:
            raise DatabaseNotConnectedError
        return self._pool

    # ── программы ────────────────────────────────────────────────────────

    async def upsert_program(self, rating: RatingData) -> Program:
        """Создаёт или обновляет программу по данным свежей загрузки."""
        record = await self.pool.fetchrow(
            """
            INSERT INTO programs (degree, financing, group_id, title, places)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (degree, financing, group_id)
            DO UPDATE SET title = EXCLUDED.title, places = EXCLUDED.places
            RETURNING id, degree, financing, group_id, title, places,
                last_update_time
            """,
            rating.degree,
            rating.financing,
            rating.group_id,
            rating.title,
            rating.places,
        )
        return _program_from(record) if record else _raise_unreachable()

    async def subscribed_programs(self) -> list[Program]:
        """Программы, на которые есть хотя бы одна подписка."""
        rows = await self.pool.fetch(
            """
            SELECT id, degree, financing, group_id, title, places,
                last_update_time
            FROM programs
            WHERE id IN (SELECT DISTINCT program_id FROM subscriptions)
            ORDER BY id
            """
        )
        return [_program_from(r) for r in rows]

    # ── снапшоты ─────────────────────────────────────────────────────────

    async def _latest_items(self, program_id: int) -> list[CompactItem] | None:
        """Содержимое последнего слепка программы; None, если слепков нет."""
        record = await self.pool.fetchrow(
            """
            SELECT items
            FROM snapshots
            WHERE program_id = $1
            ORDER BY update_time DESC
            LIMIT 1
            """,
            program_id,
        )
        return cast("list[CompactItem]", record["items"]) if record else None

    async def insert_snapshot(self, program_id: int, rating: RatingData) -> bool:
        """Сохраняет слепок; False, если ничего нового.

        Обычно новизну определяет update_time сайта (UNIQUE-конфликт).
        Если время оценено (сайт отдал update_time: null), слепок
        сохраняется только при изменении содержимого списка.
        """
        if rating.time_estimated and rating.items == await self._latest_items(
            program_id
        ):
            return False
        record = await self.pool.fetchrow(
            """
            INSERT INTO snapshots
                (program_id, update_time, total, agreements, approved,
                 paid, places, items)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (program_id, update_time) DO NOTHING
            RETURNING id
            """,
            program_id,
            rating.update_time,
            rating.total,
            rating.agreements,
            rating.approved,
            rating.paid,
            rating.places,
            rating.items,
        )
        if record is None:
            return False
        await self.pool.execute(
            """
            UPDATE programs SET last_update_time = $2
            WHERE id = $1
              AND (last_update_time IS NULL OR last_update_time < $2)
            """,
            program_id,
            rating.update_time,
        )
        return True

    async def snapshots(self, program_id: int) -> list[Snapshot]:
        """Все слепки программы по возрастанию времени."""
        rows = await self.pool.fetch(
            """
            SELECT id, program_id, update_time, fetched_at, total, agreements,
                approved, paid, places, items
            FROM snapshots
            WHERE program_id = $1
            ORDER BY update_time
            """,
            program_id,
        )
        return [_snapshot_from(r) for r in rows]

    # ── пользователи и подписки ──────────────────────────────────────────

    async def ensure_user(self, tg_id: int) -> bool:
        """Регистрирует пользователя; True — если он новый."""
        record = await self.pool.fetchrow(
            """
            INSERT INTO users (tg_id) VALUES ($1)
            ON CONFLICT DO NOTHING
            RETURNING tg_id
            """,
            tg_id,
        )
        return record is not None

    async def count_users(self) -> int:
        """Число зарегистрированных пользователей."""
        return int(await self.pool.fetchval("SELECT count(*) FROM users"))

    async def add_subscription(
        self, tg_id: int, program_id: int, sspvo_id: str
    ) -> None:
        """Добавляет подписку (идемпотентно)."""
        await self.pool.execute(
            """
            INSERT INTO subscriptions (tg_id, program_id, sspvo_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_id, program_id, sspvo_id) DO NOTHING
            """,
            tg_id,
            program_id,
            sspvo_id,
        )

    async def user_subscriptions(
        self, tg_id: int
    ) -> list[tuple[Subscription, Program]]:
        """Подписки пользователя вместе с программами."""
        rows = await self.pool.fetch(
            """
            SELECT
                s.id AS sub_id, s.tg_id, s.program_id, s.sspvo_id, s.notify,
                s.place_interval_hours, s.place_notified_at, s.last_p_base,
                p.id, p.degree, p.financing, p.group_id, p.title, p.places,
                p.last_update_time
            FROM subscriptions AS s
            JOIN programs AS p ON p.id = s.program_id
            WHERE s.tg_id = $1
            ORDER BY s.created_at
            """,
            tg_id,
        )
        return [
            (_subscription_from(r, id_key="sub_id"), _program_from(r)) for r in rows
        ]

    async def program_subscriptions(self, program_id: int) -> list[Subscription]:
        """Все подписки программы (фильтрация по флагам — на вызывающем)."""
        rows = await self.pool.fetch(
            """
            SELECT id, tg_id, program_id, sspvo_id, notify,
                place_interval_hours, place_notified_at, last_p_base
            FROM subscriptions
            WHERE program_id = $1
            """,
            program_id,
        )
        return [_subscription_from(r) for r in rows]

    async def set_place_interval(self, sub_id: int, tg_id: int, hours: int) -> bool:
        """Задаёт период дайджеста места (0 = выключить). False — не найдена."""
        result = await self.pool.execute(
            """
            UPDATE subscriptions SET place_interval_hours = $3
            WHERE id = $1 AND tg_id = $2
            """,
            sub_id,
            tg_id,
            hours,
        )
        return result.endswith("1")

    async def mark_place_notified(self, sub_id: int, at: datetime) -> None:
        """Фиксирует момент последнего дайджеста места."""
        await self.pool.execute(
            "UPDATE subscriptions SET place_notified_at = $2 WHERE id = $1",
            sub_id,
            at,
        )

    async def update_last_p(self, sub_id: int, p_base: float) -> None:
        """Сохраняет последнюю рассчитанную вероятность (для алертов)."""
        await self.pool.execute(
            "UPDATE subscriptions SET last_p_base = $2 WHERE id = $1",
            sub_id,
            p_base,
        )

    async def upsert_cross_list(self, rating: RatingData) -> None:
        """Обновляет слепок программы для кросс-анализа (хранится последний)."""
        await self.pool.execute(
            """
            INSERT INTO cross_lists
                (degree, financing, group_id, places, update_time,
                 fetched_at, items)
            VALUES ($1, $2, $3, $4, $5, now(), $6)
            ON CONFLICT (degree, financing, group_id)
            DO UPDATE SET places = EXCLUDED.places,
                update_time = EXCLUDED.update_time,
                fetched_at = EXCLUDED.fetched_at,
                items = EXCLUDED.items
            """,
            rating.degree,
            rating.financing,
            rating.group_id,
            rating.places,
            rating.update_time,
            rating.items,
        )

    async def cross_lists(self, degree: str) -> list[CrossList]:
        """Все слепки кросс-анализа для степени."""
        rows = await self.pool.fetch(
            """
            SELECT degree, financing, group_id, places, update_time,
                fetched_at, items
            FROM cross_lists
            WHERE degree = $1
            """,
            degree,
        )
        return [
            CrossList(
                degree=r["degree"],
                financing=r["financing"],
                group_id=r["group_id"],
                places=r["places"],
                update_time=r["update_time"],
                fetched_at=r["fetched_at"],
                items=cast("list[CompactItem]", r["items"]),
            )
            for r in rows
        ]

    async def upsert_lk_points(
        self, program_id: int, tg_id: int, points: list[list[object]]
    ) -> None:
        """Сохраняет точки притока из выгрузки ЛК (последняя загрузка)."""
        await self.pool.execute(
            """
            INSERT INTO lk_data (program_id, tg_id, uploaded_at, points)
            VALUES ($1, $2, now(), $3)
            ON CONFLICT (program_id)
            DO UPDATE SET tg_id = EXCLUDED.tg_id,
                uploaded_at = EXCLUDED.uploaded_at, points = EXCLUDED.points
            """,
            program_id,
            tg_id,
            points,
        )

    async def lk_points(
        self, program_id: int
    ) -> tuple[datetime, list[list[object]]] | None:
        """Точки притока из ЛК: (когда загружены, [[iso-время, балл], ...])."""
        record = await self.pool.fetchrow(
            "SELECT uploaded_at, points FROM lk_data WHERE program_id = $1",
            program_id,
        )
        if record is None:
            return None
        return record["uploaded_at"], cast("list[list[object]]", record["points"])

    async def toggle_notify(self, sub_id: int, tg_id: int) -> bool | None:
        """Переключает уведомления; None, если подписка не найдена."""
        record = await self.pool.fetchrow(
            """
            UPDATE subscriptions SET notify = NOT notify
            WHERE id = $1 AND tg_id = $2
            RETURNING notify
            """,
            sub_id,
            tg_id,
        )
        return None if record is None else record["notify"]

    async def set_notify(self, sub_id: int, *, notify: bool) -> None:
        """Явно выставляет флаг уведомлений."""
        await self.pool.execute(
            "UPDATE subscriptions SET notify = $2 WHERE id = $1",
            sub_id,
            notify,
        )

    async def delete_subscription(self, sub_id: int, tg_id: int) -> None:
        """Удаляет подписку пользователя."""
        await self.pool.execute(
            "DELETE FROM subscriptions WHERE id = $1 AND tg_id = $2",
            sub_id,
            tg_id,
        )


class UnreachableStateError(RuntimeError):
    """INSERT ... RETURNING не вернул строку — так быть не может."""

    def __init__(self) -> None:
        """Сообщение фиксировано."""
        super().__init__("INSERT ... RETURNING не вернул строку")


def _raise_unreachable() -> Program:
    raise UnreachableStateError
