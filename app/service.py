"""Сборка анализа и точек графиков из снапшотов БД."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.charts import HistoryPoint
from app.config import settings
from app.cross import CROSS_FRESH_HOURS, effective_priorities
from app.metrics import (
    MIN_HISTORY,
    Analysis,
    AnalysisContext,
    Delta,
    Horizon,
    LkPoints,
    analyze,
    diff_snapshots,
)

if TYPE_CHECKING:
    from app.db import Database
    from app.metrics import HistoryEntry
    from app.models import Program, Snapshot

MAX_CHART_POINTS = 60
DAY_AGO_HOURS = 20  # «сутки назад» с допуском на редкие обновления списка
LK_FRESH_HOURS = 48.0  # сколько часов выгрузке ЛК доверяем как «свежей»


def history_tuples(snaps: list[Snapshot]) -> list[HistoryEntry]:
    """Снапшоты в формат метрик: (update_time, items)."""
    return [(s.update_time, s.items) for s in snaps]


def _horizon() -> Horizon:
    return Horizon(enroll=settings.enroll_deadline, apply=settings.apply_deadline)


async def fetch_cross(
    db: Database, program: Program, now: datetime
) -> dict[str, int] | None:
    """Эффективные приоритеты конкурентов, если кросс-данные свежие."""
    if not settings.cross_enabled:
        return None
    rows = await db.cross_lists(program.degree)
    fresh = [
        r
        for r in rows
        if (now - r.fetched_at).total_seconds() < CROSS_FRESH_HOURS * 3600
    ]
    if not fresh:
        return None
    result = effective_priorities(
        (program.degree, program.financing, program.group_id), fresh
    )
    return result or None


async def fetch_lk(db: Database, program_id: int, now: datetime) -> LkPoints | None:
    """Свежие точки притока из выгрузки ЛК, если есть."""
    row = await db.lk_points(program_id)
    if row is None:
        return None
    uploaded_at, raw = row
    if (now - uploaded_at).total_seconds() > LK_FRESH_HOURS * 3600:
        return None
    points: list[tuple[datetime, float | None]] = []
    for entry in raw:
        stamp, score = entry[0], entry[1]
        if isinstance(stamp, str):
            points.append(
                (
                    datetime.fromisoformat(stamp),
                    float(score) if isinstance(score, (int, float)) else None,
                )
            )
    return LkPoints(uploaded_at=uploaded_at, points=points)


def build_context(
    program: Program,
    lk: LkPoints | None = None,
    cross: dict[str, int] | None = None,
) -> AnalysisContext:
    """Контекст анализа для программы."""
    return AnalysisContext(
        places=program.places,
        horizon=_horizon(),
        financing=program.financing,
        lk=lk,
        cross=cross,
    )


def build_analysis(
    snaps: list[Snapshot],
    program: Program,
    sspvo_id: str | None,
    lk: LkPoints | None = None,
    cross: dict[str, int] | None = None,
) -> Analysis:
    """Анализ последнего снапшота с учётом всей истории."""
    return analyze(
        history=history_tuples(snaps),
        sspvo_id=sspvo_id,
        ctx=build_context(program, lk, cross),
    )


def window_delta(
    snaps: list[Snapshot], sspvo_id: str | None, min_age_hours: float
) -> Delta | None:
    """Изменения последнего снапшота к ближайшему старше min_age_hours."""
    if len(snaps) < MIN_HISTORY:
        return None
    latest = snaps[-1]
    base = snaps[0]
    for s in snaps[:-1]:
        age_h = (latest.update_time - s.update_time).total_seconds() / 3600.0
        if age_h >= min_age_hours:
            base = s  # самый поздний из достаточно старых
    return diff_snapshots(
        (base.update_time, base.items),
        (latest.update_time, latest.items),
        sspvo_id,
    )


def day_delta(snaps: list[Snapshot], sspvo_id: str | None) -> Delta | None:
    """Изменения последнего снапшота к ближайшему из «суток назад»."""
    return window_delta(snaps, sspvo_id, DAY_AGO_HOURS)


def _downsample(snaps: list[Snapshot]) -> list[Snapshot]:
    if len(snaps) <= MAX_CHART_POINTS:
        return snaps
    step = len(snaps) / MAX_CHART_POINTS
    picked = [snaps[int(i * step)] for i in range(MAX_CHART_POINTS - 1)]
    picked.append(snaps[-1])
    return picked


def build_history_points(
    snaps: list[Snapshot],
    program: Program,
    sspvo_id: str | None,
    lk: LkPoints | None = None,
) -> list[HistoryPoint]:
    """Точки для графиков.

    Вероятность в каждой точке считается только по истории, доступной
    на тот момент — график показывает, как менялась оценка во времени.
    """
    snaps = _downsample(snaps)
    entries = history_tuples(snaps)
    ctx = build_context(program, lk)
    points: list[HistoryPoint] = []
    for idx, snap in enumerate(snaps):
        a = analyze(history=entries[: idx + 1], sspvo_id=sspvo_id, ctx=ctx)
        points.append(
            HistoryPoint(
                t=snap.update_time,
                total=snap.total,
                paid=snap.paid,
                approved=snap.approved,
                agreements=snap.agreements,
                position=a.position if a.found else None,
                eff_position=a.eff_position if a.found else None,
                p_base=a.p_base if a.found else None,
                p_pess=a.p_pess if a.found else None,
                p_opt=a.p_opt if a.found else None,
            )
        )
    return points
