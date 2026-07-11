"""Тесты разбора страниц конкурсных списков."""

from __future__ import annotations

import pytest

from app.fetcher import (
    NoNextDataError,
    _parse_page,
    _places_for,
    parse_program_ref,
)
from tests.fixtures import DIRECTION, T0, budget_page, flat_page, raw_item


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
    assert by_code["202"].get("q") is True
    assert by_code["203"].get("q") is False
    assert [i["pos"] for i in items] == [1, 2, 3, 4]


def test_parse_page_without_next_data() -> None:
    """Страница без __NEXT_DATA__ даёт понятную ошибку."""
    with pytest.raises(NoNextDataError):
        _parse_page("<html>пусто</html>")


def test_places_for_budget_subtracts_quotas() -> None:
    """Места общего конкурса = КЦП минус квоты."""
    direction: dict[str, object] = dict(DIRECTION)
    assert _places_for(direction, "contract") == 50
    assert _places_for(direction, "budget") == 100 - 5 - 10 - 10
