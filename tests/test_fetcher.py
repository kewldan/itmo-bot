"""Тесты разбора страниц конкурсных списков."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.fetcher import (
    NoNextDataError,
    RatingData,
    _parse_page,
    _places_for,
    fetch_rating,
    parse_program_ref,
)
from tests.fixtures import DIRECTION, T0, budget_page, flat_page, page_html, raw_item


def test_parse_program_ref_variants() -> None:
    """Ссылка разбирается в разных форматах записи."""
    expected = ("bachelor", "contract", 2340)
    assert (
        parse_program_ref("https://abit.itmo.ru/rating/bachelor/contract/2340")
        == expected
    )
    assert parse_program_ref("rating/bachelor/contract/2340?x=1") == expected
    assert parse_program_ref("привет") is None


def test_parse_flat_page() -> None:
    """Контрактный список: плоский items, сортировка по позиции."""
    items_raw = [raw_item(2, "102"), raw_item(1, "101", paid=True)]
    direction, items, update_time = _parse_page(flat_page(items_raw))
    assert update_time == T0
    assert [i["pos"] for i in items] == [1, 2]
    assert items[0]["paid"] is True
    assert items[0].get("q") is False
    assert direction["direction_title"] == DIRECTION["direction_title"]


def test_parse_budget_page_categories() -> None:
    """Бюджет: БВИ и общий конкурс без квоты, квота помечена q."""
    bvi = [raw_item(1, "201", agr=True)]
    quota = [raw_item(2, "202")]
    general = [raw_item(3, "203"), raw_item(4, "204")]
    _, items, _ = _parse_page(budget_page(bvi, quota, general))
    assert len(items) == 4
    by_code = {i["id"]: i for i in items}
    assert by_code["201"].get("q") is False
    assert by_code["201"].get("bvi") is True
    assert by_code["202"].get("q") is True
    assert by_code["203"].get("q") is False
    assert by_code["203"].get("bvi") is False
    assert [i["pos"] for i in items] == [1, 2, 3, 4]


def _null_time_page() -> str:
    """Страница с update_time: null — сайт так делает при полном списке."""
    return page_html(
        {"items": [raw_item(1, "101")], "direction": DIRECTION, "update_time": None}
    )


def _fetch_via(transport: httpx.MockTransport, financing: str) -> RatingData:
    """Синхронно скачивает список через подменённый транспорт."""

    async def _run() -> RatingData:
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_rating("bachelor", financing, 2340, client)

    return asyncio.run(_run())


def test_parse_page_null_update_time() -> None:
    """update_time: null — не ошибка разбора, список читается."""
    _, items, update_time = _parse_page(_null_time_page())
    assert update_time is None
    assert [i["id"] for i in items] == ["101"]


def test_fetch_rating_estimates_null_update_time() -> None:
    """При update_time: null подставляется время загрузки с пометкой."""
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text=_null_time_page())
    )
    rating = _fetch_via(transport, "budget")
    assert rating.time_estimated is True
    assert rating.update_time.tzinfo is not None


def test_fetch_rating_site_time_not_estimated() -> None:
    """Обычная страница: время сайта, без пометки оценки."""
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text=flat_page([raw_item(1, "101")]))
    )
    rating = _fetch_via(transport, "contract")
    assert rating.time_estimated is False
    assert rating.update_time == T0


def test_parse_page_without_next_data() -> None:
    """Страница без __NEXT_DATA__ даёт понятную ошибку."""
    with pytest.raises(NoNextDataError):
        _parse_page("<html>пусто</html>")


def test_places_for_budget_subtracts_quotas() -> None:
    """Места общего конкурса = КЦП минус квоты."""
    direction: dict[str, object] = dict(DIRECTION)
    assert _places_for(direction, "contract") == 50
    assert _places_for(direction, "budget") == 100 - 5 - 10 - 10
