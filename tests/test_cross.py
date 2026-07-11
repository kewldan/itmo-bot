"""Тесты кросс-программного анализа."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.cross import effective_priorities
from app.models import CrossList
from tests.fixtures import T0, item

if TYPE_CHECKING:
    from app.fetcher import CompactItem

TARGET = ("bachelor", "contract", 1)


def _cross_list(
    group_id: int, financing: str, places: int, items: list[CompactItem]
) -> CrossList:
    return CrossList(
        degree="bachelor",
        financing=financing,
        group_id=group_id,
        places=places,
        update_time=T0,
        fetched_at=T0,
        items=items,
    )


def test_effective_priority_counts_passing_higher_options() -> None:
    """Проходной более высокий приоритет на другой программе понижает."""
    target_items = [
        item(1, "A", prio=2),  # приоритет 1 у него на программе 2 — и проходит
        item(2, "B", prio=2),  # приоритет 1 на программе 2 — но НЕ проходит
        item(3, "C", prio=1),  # выше приоритетов нет
    ]
    other_items = [
        item(1, "A", prio=1),
        item(2, "X", prio=1),
        item(3, "B", prio=1),  # за пределами мест (places=2)
    ]
    rows = [
        _cross_list(1, "contract", 10, target_items),
        _cross_list(2, "contract", 2, other_items),
    ]
    eff = effective_priorities(TARGET, rows)
    assert eff["A"] == 2  # есть один проходной вариант приоритетнее
    assert eff["B"] == 1  # вариант приоритетнее не проходит
    assert eff["C"] == 1


def test_effective_priority_ignores_quota_rows() -> None:
    """Квотные строки не участвуют ни в целевой, ни в проходных."""
    target_items = [item(1, "A", prio=2), item(2, "A", prio=2, q=True)]
    other_items = [item(1, "A", prio=1, q=True)]
    rows = [
        _cross_list(1, "contract", 10, target_items),
        _cross_list(2, "contract", 10, other_items),
    ]
    eff = effective_priorities(TARGET, rows)
    assert eff["A"] == 1  # квотная строка не считается проходным вариантом


def test_missing_target_row_gives_empty_map() -> None:
    """Нет слепка целевой программы — нет кросс-карты."""
    rows = [_cross_list(2, "contract", 10, [item(1, "A", prio=1)])]
    assert effective_priorities(TARGET, rows) == {}
