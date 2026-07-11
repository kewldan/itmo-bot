"""Синтетические фикстуры: страницы __NEXT_DATA__ и компактные записи."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.fetcher import CompactItem

T0 = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def raw_item(
    pos: int,
    sspvo_id: str,
    total: float = 250.0,
    prio: int = 1,
    **flags: bool,
) -> dict[str, object]:
    """Запись абитуриента в формате сайта; flags: agr/app/paid."""
    return {
        "sspvo_id": sspvo_id,
        "position": pos,
        "priority": prio,
        "exam_type": "ЕГЭ",
        "total_scores": total,
        "exam_scores": total - 10,
        "ia_scores": 10,
        "is_send_agreement": flags.get("agr", False),
        "has_approved_contract": flags.get("app", False),
        "has_paid_contract": flags.get("paid", False),
    }


def page_html(program_list: dict[str, object]) -> str:
    """HTML-страница с __NEXT_DATA__."""
    payload = {"props": {"pageProps": {"programList": program_list}}}
    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


DIRECTION = {
    "direction_title": "09.03.02 «Тест»",
    "budget_min": 100,
    "contract": 50,
    "target_reception": 5,
    "invalid": 10,
    "special_quota": 10,
    "competitive_group_id": 2340,
}


def flat_page(items: list[dict[str, object]]) -> str:
    """Страница контрактного списка (плоский items)."""
    return page_html(
        {
            "items": items,
            "direction": DIRECTION,
            "update_time": T0.isoformat(),
        }
    )


def budget_page(
    bvi: list[dict[str, object]],
    quota: list[dict[str, object]],
    general: list[dict[str, object]],
) -> str:
    """Страница бюджетного списка (категории)."""
    return page_html(
        {
            "without_entry_tests": bvi,
            "by_unusual_quota": quota,
            "by_special_quota": [],
            "by_target_quota": [],
            "general_competition": general,
            "direction": DIRECTION,
            "update_time": T0.isoformat(),
        }
    )


def item(
    pos: int,
    code: str,
    prio: int = 1,
    ts: float | None = 250.0,
    **flags: bool,
) -> CompactItem:
    """Компактная запись для тестов метрик; flags: agr/app/paid/q."""
    return CompactItem(
        id=code,
        pos=pos,
        prio=prio,
        et="ЕГЭ",
        ts=ts,
        es=ts,
        ia=0.0,
        agr=flags.get("agr", False),
        app=flags.get("app", False),
        paid=flags.get("paid", False),
        q=flags.get("q", False),
    )


def hours_ago(hours: float) -> datetime:
    """Момент времени за N часов до T0."""
    return T0 - timedelta(hours=hours)
