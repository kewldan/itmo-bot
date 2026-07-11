"""Загрузка и разбор конкурсного списка abit.itmo.ru.

Страница рендерится Next.js и содержит полный список абитуриентов
в теге <script id="__NEXT_DATA__"> — API и авторизация не нужны.
Поддерживаются любые направления: бакалавриат/магистратура/аспирантура,
бюджет/контракт/целевая.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import TypedDict, cast

import httpx

from app.config import settings

BASE_URL = "https://abit.itmo.ru/rating/{degree}/{financing}/{group_id}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) itmo-rating-bot/1.0"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_URL_RE = re.compile(
    r"(?:https?://)?(?:abit\.itmo\.ru)?/?rating/"
    r"(?P<degree>[a-z_]+)/(?P<financing>[a-z_]+)/(?P<group_id>\d+)"
)


class FetchError(Exception):
    """Базовая ошибка загрузки конкурсного списка."""


class RatingNotFoundError(FetchError):
    """Страница списка не существует (HTTP 404)."""

    def __init__(self, url: str) -> None:
        """Формирует сообщение по адресу списка."""
        super().__init__(f"Список {url} не найден (404) — проверьте ссылку.")


class RatingHttpError(FetchError):
    """Сайт ответил неожиданным HTTP-статусом."""

    def __init__(self, url: str, status: int) -> None:
        """Формирует сообщение по адресу и статусу."""
        super().__init__(f"Сайт вернул HTTP {status} для {url}.")


class RatingParseError(FetchError):
    """Не удалось извлечь данные из страницы."""

    def __init__(self, reason: str) -> None:
        """Формирует сообщение по причине сбоя разбора."""
        super().__init__(f"Не удалось разобрать страницу списка: {reason}")


class NoNextDataError(FetchError):
    """На странице нет __NEXT_DATA__ — вероятно, сайт изменился."""

    def __init__(self) -> None:
        """Сообщение фиксировано."""
        super().__init__("На странице нет данных — вероятно, сайт изменился.")


class CompactItem(TypedDict):
    """Компактная запись абитуриента в снапшоте."""

    id: str
    pos: int | None
    prio: int | None
    et: str | None
    ts: float | None
    es: float | None
    ia: float | None
    agr: bool
    app: bool
    paid: bool


@dataclass(frozen=True, slots=True)
class RatingData:
    """Разобранный конкурсный список."""

    degree: str
    financing: str
    group_id: int
    title: str
    places: int
    update_time: datetime
    items: list[CompactItem]

    @property
    def total(self) -> int:
        """Число заявлений в списке."""
        return len(self.items)

    @property
    def agreements(self) -> int:
        """Число поданных согласий на зачисление."""
        return sum(1 for i in self.items if i["agr"])

    @property
    def approved(self) -> int:
        """Число одобренных договоров."""
        return sum(1 for i in self.items if i["app"])

    @property
    def paid(self) -> int:
        """Число оплаченных договоров."""
        return sum(1 for i in self.items if i["paid"])


def parse_program_ref(text: str) -> tuple[str, str, int] | None:
    """Разбирает ссылку вида abit.itmo.ru/rating/bachelor/contract/2340."""
    match = _URL_RE.search(text.strip())
    if match is None:
        return None
    return (
        match.group("degree"),
        match.group("financing"),
        int(match.group("group_id")),
    )


def _to_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _to_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) else None


def _compact(item: dict[str, object]) -> CompactItem:
    return CompactItem(
        id=str(item.get("sspvo_id") or ""),
        pos=_to_int(item.get("position")),
        prio=_to_int(item.get("priority")),
        et=str(item["exam_type"]) if item.get("exam_type") else None,
        ts=_to_float(item.get("total_scores")),
        es=_to_float(item.get("exam_scores")),
        ia=_to_float(item.get("ia_scores")),
        agr=bool(item.get("is_send_agreement")),
        app=bool(item.get("has_approved_contract")),
        paid=bool(item.get("has_paid_contract")),
    )


def _places_for(direction: dict[str, object], financing: str) -> int:
    """Число мест для вида финансирования.

    Для бюджета из КЦП вычитаются квоты (целевая, особая, отдельная):
    приближение общего конкурса.
    """
    if financing == "contract":
        return _to_int(direction.get("contract")) or 0
    if financing == "budget":
        budget = _to_int(direction.get("budget_min")) or 0
        quotas = sum(
            _to_int(direction.get(key)) or 0
            for key in ("target_reception", "invalid", "special_quota")
        )
        return max(budget - quotas, 0)
    return _to_int(direction.get(financing)) or 0


def _parse_page(html: str) -> tuple[dict[str, object], list[CompactItem], datetime]:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise NoNextDataError
    try:
        data = json.loads(match.group(1))
        program_list = data["props"]["pageProps"]["programList"]
        direction = cast("dict[str, object]", program_list["direction"])
        raw_items = cast("list[dict[str, object]]", program_list["items"])
        items = [_compact(i) for i in raw_items]
        items.sort(key=lambda i: (i["pos"] is None, i["pos"] or 0))
        update_time = datetime.fromisoformat(str(program_list["update_time"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RatingParseError(str(exc)) from exc
    return direction, items, update_time


async def fetch_rating(
    degree: str,
    financing: str,
    group_id: int,
    client: httpx.AsyncClient | None = None,
) -> RatingData:
    """Скачивает и разбирает конкурсный список."""
    url = BASE_URL.format(degree=degree, financing=financing, group_id=group_id)
    if client is None:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as own_client:
            response = await own_client.get(
                url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
    else:
        response = await client.get(
            url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    if response.status_code == HTTPStatus.NOT_FOUND:
        raise RatingNotFoundError(url)
    if response.status_code != HTTPStatus.OK:
        raise RatingHttpError(url, response.status_code)

    direction, items, update_time = _parse_page(response.text)
    title = str(direction.get("direction_title") or f"Группа {group_id}")
    return RatingData(
        degree=degree,
        financing=financing,
        group_id=group_id,
        title=title,
        places=_places_for(direction, financing),
        update_time=update_time,
        items=items,
    )
