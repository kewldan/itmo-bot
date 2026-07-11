"""Тесты справочника итогов 2025."""

from __future__ import annotations

from app.history2025 import for_title


def test_lookup_by_code_in_title() -> None:
    """Код направления извлекается из названия программы."""
    hist = for_title("09.03.02 «Информационные системы и технологии»")
    assert hist is not None
    assert hist.contract == 289
    assert hist.budget_general == 0
    assert hist.bvi == 161


def test_unknown_title_gives_none() -> None:
    """Неизвестное направление — нет данных."""
    assert for_title("99.99.99 «Неизвестно»") is None
    assert for_title("без кода") is None
