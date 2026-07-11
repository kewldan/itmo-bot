"""Кросс-программный анализ конкурентов.

Код ЕПГУ один и тот же во всех конкурсных списках. Если конкурент выше
вас имеет на другой программе более высокий приоритет и проходит туда,
ваше место он, скорее всего, не займёт. Фоновый обход собирает последние
слепки всех списков степени; по ним считается «эффективный приоритет»
каждого конкурента на целевой программе: 1 + число более приоритетных
программ, куда он проходит. Эффективный приоритет заменяет заявленный
в приорах модели.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from app.config import settings
from app.fetcher import FetchError, fetch_direction_ids, fetch_rating

if TYPE_CHECKING:
    from app.db import Database
    from app.fetcher import CompactItem
    from app.models import CrossList

log = logging.getLogger(__name__)

CROSS_FRESH_HOURS = 12.0
_SWEEP_PAUSE_SECONDS = 1.5
_FINANCINGS = ("budget", "contract")


def _pass_ids(items: list[CompactItem], places: int) -> set[str]:
    """Кто проходит на программу: первые places неквотных позиций."""
    non_quota = sorted(
        (i for i in items if not i.get("q") and i["pos"] is not None),
        key=lambda i: i["pos"] or 0,
    )
    return {i["id"] for i in non_quota[: max(places, 0)] if i["id"]}


def effective_priorities(
    target: tuple[str, str, int], rows: list[CrossList]
) -> dict[str, int]:
    """Эффективный приоритет каждого конкурента на целевой программе."""
    target_row = next(
        (r for r in rows if (r.degree, r.financing, r.group_id) == target), None
    )
    if target_row is None:
        return {}
    others = [r for r in rows if (r.degree, r.financing, r.group_id) != target]
    passes: list[tuple[set[str], dict[str, int]]] = []
    for row in others:
        prio_map = {
            i["id"]: prio
            for i in row.items
            if i["id"] and not i.get("q") and (prio := i["prio"]) is not None
        }
        passes.append((_pass_ids(row.items, row.places), prio_map))

    result: dict[str, int] = {}
    for item in target_row.items:
        code, my_prio = item["id"], item["prio"]
        if item.get("q") or not code or my_prio is None:
            continue
        better = sum(
            1
            for pass_ids, prio_map in passes
            if (p := prio_map.get(code)) is not None
            and p < my_prio
            and code in pass_ids
        )
        result[code] = 1 + better
    return result


async def cross_once(db: Database) -> None:
    """Один обход всех списков степеней, на которые есть подписки."""
    degrees = {p.degree for p in await db.subscribed_programs()}
    if not degrees:
        return
    fetched = 0
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        for degree in sorted(degrees):
            try:
                group_ids = await fetch_direction_ids(degree, client)
            except (FetchError, httpx.HTTPError) as exc:
                log.warning("Кросс-обход: нет списка направлений %s: %s", degree, exc)
                continue
            for group_id in group_ids:
                for financing in _FINANCINGS:
                    try:
                        rating = await fetch_rating(degree, financing, group_id, client)
                    except (FetchError, httpx.HTTPError):
                        continue  # не у каждой группы есть оба списка
                    await db.upsert_cross_list(rating)
                    fetched += 1
                    await asyncio.sleep(_SWEEP_PAUSE_SECONDS)
    log.info("Кросс-обход завершён: %d списков", fetched)


async def cross_loop(db: Database) -> None:
    """Периодический обход; переживает любые сбои итерации."""
    if not settings.cross_enabled:
        log.info("Кросс-анализ выключен (CROSS_ENABLED=false)")
        return
    log.info("Кросс-обход запущен, период %.1f ч", settings.cross_poll_hours)
    while True:
        results = await asyncio.gather(cross_once(db), return_exceptions=True)
        error = results[0]
        if isinstance(error, BaseException):
            log.error("Сбой кросс-обхода", exc_info=error)
        await asyncio.sleep(settings.cross_poll_hours * 3600)
