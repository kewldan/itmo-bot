"""Тесты сервисного слоя (без БД)."""

from __future__ import annotations

from app.models import Snapshot
from app.service import window_delta
from tests.fixtures import T0, hours_ago, item


def _snapshot(snap_id: int, hours: float, codes: list[str]) -> Snapshot:
    items = [item(pos, code) for pos, code in enumerate(codes, start=1)]
    return Snapshot(
        id=snap_id,
        program_id=1,
        update_time=hours_ago(hours),
        fetched_at=hours_ago(hours),
        total=len(items),
        agreements=0,
        approved=0,
        paid=0,
        places=50,
        items=items,
    )


def test_window_delta_picks_oldest_within_window() -> None:
    """База сравнения — самый поздний снапшот старше окна."""
    snaps = [
        _snapshot(1, 30, ["1"]),
        _snapshot(2, 8, ["1", "2"]),
        _snapshot(3, 0, ["1", "2", "3"]),
    ]
    delta = window_delta(snaps, None, min_age_hours=6)
    assert delta is not None
    assert delta.d_total == 1  # сравнение с снапшотом 8 часов назад
    delta_day = window_delta(snaps, None, min_age_hours=24)
    assert delta_day is not None
    assert delta_day.d_total == 2  # сравнение с 30-часовым

    assert window_delta(snaps[:1], None, min_age_hours=6) is None
    assert snaps[-1].update_time == T0
