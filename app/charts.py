"""Графики динамики: PNG для отправки в Telegram.

Палитра и правила — по data-viz гайду: фиксированный порядок категориальных
цветов, тонкие линии, ненавязчивая сетка, прямые подписи серий.
Бэкенд Agg задаётся переменной окружения в app/__init__.py.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from app.config import MSK

if TYPE_CHECKING:
    from datetime import datetime

    from matplotlib.axes import Axes

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
S1, S2, S3 = "#2a78d6", "#1baf7a", "#eda100"  # категориальные слоты 1–3
BAND = "#cde2fb"  # sequential blue 100 — фон коридора сценариев

MAX_MARKER_POINTS = 25
PROB_YLIM_PAD = 5.0
PROB_YLIM_MIN_TOP = 95.0


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """Точка истории для графиков."""

    t: datetime
    total: int
    paid: int
    approved: int
    agreements: int
    position: int | None
    eff_position: float | None
    p_base: float | None
    p_pess: float | None
    p_opt: float | None


def _style_ax(ax: Axes) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(visible=True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M", tz=MSK))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))


def _plot(
    ax: Axes,
    xs: list[float],
    ys: list[float],
    color: str,
    label: str | None = None,
) -> None:
    marker = "o" if len(xs) <= MAX_MARKER_POINTS else None
    ax.plot(
        xs,
        ys,
        color=color,
        linewidth=2,
        label=label,
        marker=marker,
        markersize=4.5,
    )


def _draw_total(
    ax: Axes, xs: list[float], points: list[HistoryPoint], places: int
) -> None:
    ax.set_title("Заявлений в списке", color=INK_2, fontsize=11, loc="left")
    _plot(ax, xs, [float(p.total) for p in points], S1)
    ax.axhline(places, color=MUTED, linewidth=1, linestyle="--")
    ax.annotate(
        f"мест: {places}",
        (0.99, places),
        xycoords=("axes fraction", "data"),
        ha="right",
        va="bottom",
        color=MUTED,
        fontsize=9,
    )


def _draw_position(ax: Axes, xs: list[float], points: list[HistoryPoint]) -> None:
    ax.set_title("Ваше место (меньше — лучше)", color=INK_2, fontsize=11, loc="left")
    pos = [
        (x, float(p.position))
        for x, p in zip(xs, points, strict=True)
        if p.position is not None
    ]
    eff = [
        (x, p.eff_position)
        for x, p in zip(xs, points, strict=True)
        if p.eff_position is not None
    ]
    if not pos and not eff:
        ax.text(
            0.5,
            0.5,
            "нет данных о вашей позиции",
            transform=ax.transAxes,
            ha="center",
            color=MUTED,
            fontsize=10,
        )
        return
    if pos:
        _plot(ax, [x for x, _ in pos], [y for _, y in pos], S1, "место в списке")
    if eff:
        _plot(ax, [x for x, _ in eff], [y for _, y in eff], S2, "эффективная позиция")
    ax.invert_yaxis()
    ax.legend(loc="best", fontsize=9, frameon=False, labelcolor=INK_2)


def _draw_contracts(ax: Axes, xs: list[float], points: list[HistoryPoint]) -> None:
    ax.set_title("Договоры и согласия", color=INK_2, fontsize=11, loc="left")
    _plot(ax, xs, [float(p.paid) for p in points], S1, "оплачен")
    _plot(ax, xs, [float(p.approved) for p in points], S2, "одобрен")
    _plot(ax, xs, [float(p.agreements) for p in points], S3, "согласие")
    ax.legend(loc="best", fontsize=9, frameon=False, labelcolor=INK_2)


def _draw_probability(ax: Axes, xs: list[float], points: list[HistoryPoint]) -> None:
    ax.set_title("Вероятность поступления, %", color=INK_2, fontsize=11, loc="left")
    prob = [(x, p) for x, p in zip(xs, points, strict=True) if p.p_base is not None]
    if not prob:
        ax.text(
            0.5,
            0.5,
            "нет данных",
            transform=ax.transAxes,
            ha="center",
            color=MUTED,
            fontsize=10,
        )
        return
    pxs = [x for x, _ in prob]
    base = [(p.p_base or 0.0) * 100 for _, p in prob]
    lo = [(p.p_pess or 0.0) * 100 for _, p in prob]
    hi = [(p.p_opt or 0.0) * 100 for _, p in prob]
    ax.fill_between(pxs, lo, hi, color=BAND, alpha=0.7, linewidth=0)
    _plot(ax, pxs, base, S1)
    bottom = min([*lo, *base])
    ax.set_ylim(max(min(bottom - PROB_YLIM_PAD, PROB_YLIM_MIN_TOP), 0.0), 100.5)
    ax.annotate(
        "коридор: песс.–опт. сценарии",
        (0.01, 0.03),
        xycoords="axes fraction",
        color=MUTED,
        fontsize=8.5,
    )


def render_history(points: list[HistoryPoint], places: int, title: str) -> bytes:
    """Рисует панель 2×2 и возвращает PNG."""
    # matplotlib-даты (float): стабы Axes.plot не принимают list[datetime]
    xs = [float(v) for v in mdates.date2num([p.t.astimezone(MSK) for p in points])]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5), dpi=140)
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(title, color=INK, fontsize=13, fontweight="bold", y=0.985)
    (ax1, ax2), (ax3, ax4) = axes
    for ax in (ax1, ax2, ax3, ax4):
        _style_ax(ax)

    _draw_total(ax1, xs, points, places)
    _draw_position(ax2, xs, points)
    _draw_contracts(ax3, xs, points)
    _draw_probability(ax4, xs, points)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
