"""Доменные модели — строки таблиц БД в виде датаклассов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from app.fetcher import CompactItem


@dataclass(frozen=True, slots=True)
class Program:
    """Конкурсная группа (направление × вид финансирования)."""

    id: int
    degree: str
    financing: str
    group_id: int
    title: str
    places: int
    last_update_time: datetime | None

    @property
    def url(self) -> str:
        """Ссылка на страницу конкурсного списка."""
        return (
            "https://abit.itmo.ru/rating/"
            f"{self.degree}/{self.financing}/{self.group_id}"
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Слепок конкурсного списка на момент update_time сайта."""

    id: int
    program_id: int
    update_time: datetime
    fetched_at: datetime
    total: int
    agreements: int
    approved: int
    paid: int
    places: int
    items: list[CompactItem]


@dataclass(frozen=True, slots=True)
class Subscription:
    """Подписка пользователя: программа + его код ЕПГУ.

    notify — уведомления об изменениях списка;
    place_interval_hours — периодический дайджест места (0 = выключен),
    place_notified_at — когда дайджест отправлялся в последний раз.
    """

    id: int
    tg_id: int
    program_id: int
    sspvo_id: str
    notify: bool
    place_interval_hours: int
    place_notified_at: datetime | None
