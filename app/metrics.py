"""Модель вероятности поступления.

Место в списке само по себе мало значит: значительная часть заявлений —
«фантомы» (людей, которые уйдут на другие программы). Модель отвечает
на вопрос «сколько человек выше меня по баллу реально займут места
и хватит ли мест после них». Три слоя:

1. «В моменте»: каждому конкуренту выше по списку присваивается вероятность
   реально занять место — по его сигналам (оплатил договор > одобрен >
   согласие > голая заявка с приоритетом N). Число занятых мест выше вас —
   Пуассон-биномиальная величина X; P(поступления) = P(X <= мест - 1),
   считается точным динамическим программированием, без симуляций.

2. Калибровка по истории: приоры «согласие -> оплата» и т.п. уточняются
   наблюдаемыми конверсиями в снапшотах этой же программы. Байесовское
   сглаживание: при малой выборке работает приор, при большой — данные.
   Конверсия экстраполируется на оставшееся до дедлайна время
   в предположении постоянной интенсивности.

3. Приток: по истории оценивается скорость подачи новых заявлений и доля
   новичков с баллом выше вашего; ожидаемое число новых реальных
   конкурентов к дедлайну — Пуассон, свёртка с X из п. 1.

Сценарии: базовый / пессимистичный / оптимистичный — сдвиг вероятностей
конкурентов в шансах (odds) и масштаба притока.

Для контрактных списков параметры откалиброваны лучше всего; для бюджетных
используются свои приоры, и оценка помечается как приближённая (модель
не решает задачу распределения по приоритетам между программами).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Literal

from app.config import MSK

if TYPE_CHECKING:
    from collections.abc import Iterator

    from app.fetcher import CompactItem

type HistoryEntry = tuple[datetime, list["CompactItem"]]
type StateKey = Literal["paid", "approved", "agreement", "none"]

# ── Параметры модели по виду финансирования ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelParams:
    """Приоры вероятности «человек реально займёт место»."""

    p_paid: float
    p_approved: float
    p_agreement: float
    p_by_priority: dict[int, float]
    p_no_priority: float
    approximate: bool  # честная пометка: модель для этого списка грубее


CONTRACT_PARAMS = ModelParams(
    p_paid=0.97,  # оплатил: почти наверняка (редкие отзывы документов)
    p_approved=0.85,  # договор одобрен, но не оплачен
    p_agreement=0.70,  # подал согласие на зачисление
    p_by_priority={1: 0.30, 2: 0.18, 3: 0.12, 4: 0.08, 5: 0.06},
    p_no_priority=0.15,
    approximate=False,
)
BUDGET_PARAMS = ModelParams(
    p_paid=0.97,
    p_approved=0.90,
    p_agreement=0.90,  # на бюджете согласие — сильный сигнал
    p_by_priority={1: 0.75, 2: 0.45, 3: 0.30, 4: 0.20, 5: 0.12},
    p_no_priority=0.35,
    approximate=True,
)


def params_for(financing: str) -> ModelParams:
    """Параметры модели для вида финансирования."""
    return CONTRACT_PARAMS if financing == "contract" else BUDGET_PARAMS


# Калибровка: вес приора в псевдонаблюдениях и минимальный возраст истории.
CALIB_PSEUDO_N = 40
MIN_CALIB_HOURS = 12.0
INFLUX_WINDOW_HOURS = 96.0
MIN_HISTORY = 2
NEWCOMER_SAMPLE_MIN = 10
_MAX_EXP_ARG = 700.0
_P_CAP = 0.99
_P_FLOOR = 0.01
_MU_EPSILON = 1e-9
# Зажим поправки к шансам: короткое окно наблюдений не должно
# ни обнулять приор, ни раздувать его до крайностей.
_ODDS_FACTOR_MIN = 0.2
_ODDS_FACTOR_MAX = 5.0


@dataclass(frozen=True, slots=True)
class Horizon:
    """Ключевые даты кампании.

    enroll — дедлайн оплаты договора (горизонт конверсий и оплат);
    apply — конец приёма заявлений (горизонт притока новых конкурентов).
    """

    enroll: date
    apply: date


# Сценарии: множитель шансов конкурентов и множитель притока.
SCENARIOS: dict[str, tuple[float, float]] = {
    "base": (1.0, 1.0),
    "pess": (2.0, 1.6),
    "opt": (0.5, 0.5),
}


def _state_key(item: CompactItem) -> StateKey:
    if item["paid"]:
        return "paid"
    if item["app"]:
        return "approved"
    if item["agr"]:
        return "agreement"
    return "none"


def prior_enroll(item: CompactItem, params: ModelParams) -> float:
    """Приор вероятности занять место — по сигналам абитуриента."""
    state = _state_key(item)
    if state == "paid":
        return params.p_paid
    if state == "approved":
        return params.p_approved
    if state == "agreement":
        return params.p_agreement
    prio = item["prio"]
    if prio is None:
        return params.p_no_priority
    return params.p_by_priority.get(prio, params.p_no_priority)


def _scale_odds(p: float, factor: float) -> float:
    """Масштабирует вероятность в шансах, с зажимом в (0, 0.99]."""
    if p <= 0.0 or p >= 1.0 or factor == 1.0:
        return min(max(p, 0.0), _P_CAP)
    odds = p / (1.0 - p) * factor
    return min(odds / (1.0 + odds), _P_CAP)


# ── Точная Пуассон-биномиальная свёртка ──────────────────────────────────────


def _poisson_binomial_pmf(ps: list[float], cap: int) -> list[float]:
    """Распределение числа успехов: индекс cap накапливает P(X >= cap)."""
    dp = [0.0] * (cap + 1)
    dp[0] = 1.0
    for p in ps:
        q = 1.0 - p
        dp[cap] += dp[cap - 1] * p
        for k in range(cap - 1, 0, -1):
            dp[k] = dp[k] * q + dp[k - 1] * p
        dp[0] *= q
    return dp


def _poisson_terms(mu: float, k_max: int) -> Iterator[tuple[int, float]]:
    """Члены пуассоновского распределения P(K = k) для k = 0..k_max."""
    term = math.exp(-mu) if mu < _MAX_EXP_ARG else 0.0
    for k in range(k_max + 1):
        yield k, term
        term = term * mu / (k + 1)


def _prob_admission(ps_ahead: list[float], places: int, mu_new: float) -> float:
    """P(занятых мест выше < мест), с притоком новых ~ Poisson(mu_new)."""
    if places <= 0:
        return 0.0
    pmf = _poisson_binomial_pmf(ps_ahead, places)
    cdf = [0.0] * places
    acc = 0.0
    for j in range(places):
        acc += pmf[j]
        cdf[j] = acc
    if mu_new <= _MU_EPSILON:
        return cdf[places - 1]
    return sum(pk * cdf[places - 1 - k] for k, pk in _poisson_terms(mu_new, places - 1))


# ── Калибровка по истории снапшотов ──────────────────────────────────────────


def _default_odds() -> dict[StateKey, float]:
    return {"agreement": 1.0, "approved": 1.0, "none": 1.0}


@dataclass(slots=True)
class Calibration:
    """Поправки (множители шансов) к приорам по наблюдаемым конверсиям."""

    odds_factor: dict[StateKey, float] = field(default_factory=_default_odds)
    observed_hours: float = 0.0
    enough_data: bool = False


def _hours(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 3600.0


def _cohort_prior(
    state: StateKey, cohort: list[CompactItem], params: ModelParams
) -> float:
    if state == "approved":
        return params.p_approved
    if state == "agreement":
        return params.p_agreement
    if not cohort:
        return params.p_no_priority
    return sum(prior_enroll(i, params) for i in cohort) / len(cohort)


def calibrate(
    history: list[HistoryEntry], deadline_at: datetime, params: ModelParams
) -> Calibration:
    """Уточняет приоры конверсиями «состояние -> оплата» из истории.

    Наблюдаемая конверсия экстраполируется на весь горизонт до дедлайна
    (постоянная интенсивность) и байесовски смешивается с приором.
    Вес данных пропорционален не только размеру когорты, но и доле уже
    прошедшего горизонта: 12 часов без конверсий не должны перечёркивать
    приор о поведении людей за месяц.
    """
    calib = Calibration()
    if len(history) < MIN_HISTORY:
        return calib
    early_t, early_items = history[0]
    late_t, late_items = history[-1]
    elapsed_h = _hours(late_t, early_t)
    if elapsed_h < MIN_CALIB_HOURS:
        return calib
    calib.observed_hours = elapsed_h
    calib.enough_data = True

    remain_h = max(_hours(deadline_at, late_t), 0.0)
    horizon_ratio = (elapsed_h + remain_h) / elapsed_h
    elapsed_share = elapsed_h / (elapsed_h + remain_h)
    late_by_id = {i["id"]: i for i in late_items if i["id"]}

    for state in ("agreement", "approved", "none"):
        cohort = [i for i in early_items if i["id"] and _state_key(i) == state]
        if not cohort:
            continue
        converted = sum(
            1 for i in cohort if i["id"] in late_by_id and late_by_id[i["id"]]["paid"]
        )
        c_obs = converted / len(cohort)
        c_proj = 1.0 - (1.0 - min(c_obs, _P_CAP)) ** horizon_ratio
        prior = _cohort_prior(state, cohort, params)
        n_eff = len(cohort) * elapsed_share
        weight = n_eff / (n_eff + CALIB_PSEUDO_N)
        p_blend = weight * c_proj + (1.0 - weight) * prior
        p_blend = min(max(p_blend, _P_FLOOR), _P_CAP)
        if 0.0 < prior < 1.0:
            factor = (p_blend / (1 - p_blend)) / (prior / (1 - prior))
            calib.odds_factor[state] = min(
                max(factor, _ODDS_FACTOR_MIN), _ODDS_FACTOR_MAX
            )
    return calib


def calibrated_enroll(
    item: CompactItem, calib: Calibration, params: ModelParams
) -> float:
    """Вероятность занять место с учётом калибровки."""
    p = prior_enroll(item, params)
    state = _state_key(item)
    if state == "paid":
        return p
    return _scale_odds(p, calib.odds_factor[state])


# ── Приток новых заявлений ───────────────────────────────────────────────────


@dataclass(slots=True)
class Influx:
    """Оценка скорости притока заявлений и оплат."""

    rate_per_day: float = 0.0
    paid_rate_per_day: float = 0.0
    q_ahead: float = 0.0  # доля новичков с баллом выше вашего
    enough_data: bool = False


def _q_ahead(
    newcomers: list[CompactItem], late_items: list[CompactItem], my_score: float
) -> float:
    pool = newcomers if len(newcomers) >= NEWCOMER_SAMPLE_MIN else late_items
    scored = [i["ts"] for i in pool if i["ts"] is not None]
    if not scored:
        return 0.0
    above = sum(1 for s in scored if s > my_score)
    ties = sum(1 for s in scored if s == my_score)
    return (above + 0.5 * ties) / len(scored)


def estimate_influx(history: list[HistoryEntry], my_score: float | None) -> Influx:
    """Оценивает приток по окну последних снапшотов."""
    influx = Influx()
    if len(history) < MIN_HISTORY:
        return influx
    late_t, late_items = history[-1]
    window = [
        (t, items) for t, items in history if _hours(late_t, t) <= INFLUX_WINDOW_HOURS
    ]
    early_t, early_items = window[0]
    elapsed_h = _hours(late_t, early_t)
    if elapsed_h < MIN_CALIB_HOURS:
        return influx
    influx.enough_data = True
    days = elapsed_h / 24.0

    early_ids = {i["id"] for i in early_items if i["id"]}
    newcomers = [i for i in late_items if i["id"] and i["id"] not in early_ids]
    influx.rate_per_day = len(newcomers) / days

    paid_early = sum(1 for i in early_items if i["paid"])
    paid_late = sum(1 for i in late_items if i["paid"])
    influx.paid_rate_per_day = max(paid_late - paid_early, 0) / days

    if my_score is not None:
        influx.q_ahead = _q_ahead(newcomers, late_items, my_score)
    return influx


# ── Итоговый анализ ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class AheadBreakdown:
    """Разбивка конкурентов выше вас по статусам."""

    paid: int = 0
    approved: int = 0
    agreement: int = 0
    none: int = 0
    none_prio1: int = 0


@dataclass(slots=True)
class Analysis:
    """Полный разбор конкурсной ситуации для одного абитуриента."""

    found: bool
    total: int
    places: int
    paid: int
    approved: int
    agreements: int
    update_time: datetime
    approximate: bool
    position: int | None = None
    score: float | None = None
    exam_score: float | None = None
    ia_score: float | None = None
    priority: int | None = None
    percentile: float | None = None  # доля списка строго ниже вас по баллу
    ahead: AheadBreakdown = field(default_factory=AheadBreakdown)
    mu_ahead_now: float = 0.0  # ожидаемое число реальных конкурентов выше
    eff_position: float = 0.0
    p_now: float = 0.0  # если бы приём закрылся сегодня
    p_base: float = 0.0  # прогноз к дедлайну
    p_pess: float = 0.0
    p_opt: float = 0.0
    mu_new_ahead: float = 0.0  # ожидание новых конкурентов выше к дедлайну
    days_left: float = 0.0  # до дедлайна оплаты
    days_apply: float = 0.0  # до конца приёма заявлений
    influx: Influx = field(default_factory=Influx)
    calib: Calibration = field(default_factory=Calibration)
    forecast_total: float = 0.0
    forecast_paid: float = 0.0
    my_state: StateKey = "none"


def deadline_at(day: date) -> datetime:
    """Момент дедлайна: конец дня по Москве."""
    return datetime.combine(day, time(23, 59), tzinfo=MSK)


def _totals(items: list[CompactItem]) -> tuple[int, int, int]:
    paid = sum(1 for i in items if i["paid"])
    approved = sum(1 for i in items if i["app"])
    agreements = sum(1 for i in items if i["agr"])
    return paid, approved, agreements


def _breakdown(ahead_items: list[CompactItem]) -> AheadBreakdown:
    b = AheadBreakdown()
    for item in ahead_items:
        state = _state_key(item)
        if state == "paid":
            b.paid += 1
        elif state == "approved":
            b.approved += 1
        elif state == "agreement":
            b.agreement += 1
        else:
            b.none += 1
            if item["prio"] == 1:
                b.none_prio1 += 1
    return b


def _fill_personal(a: Analysis, me: CompactItem, items: list[CompactItem]) -> None:
    a.found = True
    a.position = me["pos"]
    a.score = me["ts"]
    a.exam_score = me["es"]
    a.ia_score = me["ia"]
    a.priority = me["prio"]
    a.my_state = _state_key(me)
    scored = [i["ts"] for i in items if i["ts"] is not None]
    my_ts = me["ts"]
    if my_ts is not None and scored:
        a.percentile = sum(1 for s in scored if s < my_ts) / len(scored)


def _fill_probabilities(
    a: Analysis,
    ahead_items: list[CompactItem],
    params: ModelParams,
) -> None:
    ps_base = [calibrated_enroll(i, a.calib, params) for i in ahead_items]
    a.mu_ahead_now = sum(ps_base)
    a.eff_position = a.mu_ahead_now + 1.0

    p_new_mean = sum(params.p_by_priority.values()) / len(params.p_by_priority)
    a.mu_new_ahead = (
        a.influx.rate_per_day * a.days_apply * a.influx.q_ahead * p_new_mean
    )

    a.p_now = _prob_admission(ps_base, a.places, 0.0)
    for name, (odds_factor, influx_factor) in SCENARIOS.items():
        ps = [
            p if item["paid"] else _scale_odds(p, odds_factor)
            for p, item in zip(ps_base, ahead_items, strict=True)
        ]
        value = _prob_admission(ps, a.places, a.mu_new_ahead * influx_factor)
        setattr(a, f"p_{name}", value)


def analyze(
    history: list[HistoryEntry],
    sspvo_id: str | None,
    places: int,
    horizon: Horizon,
    financing: str,
) -> Analysis:
    """Полный анализ по истории снапшотов (последний — текущее состояние)."""
    update_time, items = history[-1]
    params = params_for(financing)
    paid, approved, agreements = _totals(items)
    a = Analysis(
        found=False,
        total=len(items),
        places=places,
        paid=paid,
        approved=approved,
        agreements=agreements,
        update_time=update_time,
        approximate=params.approximate,
    )
    me = next((i for i in items if sspvo_id and i["id"] == str(sspvo_id)), None)
    if me is None:
        return a

    _fill_personal(a, me, items)
    finish = deadline_at(horizon.enroll)
    apply_end = deadline_at(horizon.apply)
    a.days_left = max((finish - update_time).total_seconds() / 86400.0, 0.0)
    a.days_apply = min(
        max((apply_end - update_time).total_seconds() / 86400.0, 0.0), a.days_left
    )
    a.calib = calibrate(history, finish, params)
    a.influx = estimate_influx(history, me["ts"])

    my_pos = me["pos"]
    ahead_items = [
        i
        for i in items
        if i["pos"] is not None and my_pos is not None and i["pos"] < my_pos
    ]
    a.ahead = _breakdown(ahead_items)
    _fill_probabilities(a, ahead_items, params)

    a.forecast_total = a.total + a.influx.rate_per_day * a.days_apply
    a.forecast_paid = min(
        a.paid + a.influx.paid_rate_per_day * a.days_left, float(a.total)
    )
    return a


# ── Дельты между снапшотами (для сообщений и уведомлений) ────────────────────


@dataclass(frozen=True, slots=True)
class Delta:
    """Изменения между двумя снапшотами."""

    hours: float
    d_total: int
    d_paid: int
    d_approved: int
    d_agreements: int
    d_position: int | None  # отрицательное = поднялись вверх


def _position_of(items: list[CompactItem], sspvo_id: str | None) -> int | None:
    if not sspvo_id:
        return None
    me = next((i for i in items if i["id"] == str(sspvo_id)), None)
    return me["pos"] if me else None


def diff_snapshots(old: HistoryEntry, new: HistoryEntry, sspvo_id: str | None) -> Delta:
    """Считает изменения списка между двумя снапшотами."""
    (old_t, old_items), (new_t, new_items) = old, new
    old_pos = _position_of(old_items, sspvo_id)
    new_pos = _position_of(new_items, sspvo_id)
    old_paid, old_app, old_agr = _totals(old_items)
    new_paid, new_app, new_agr = _totals(new_items)
    return Delta(
        hours=_hours(new_t, old_t),
        d_total=len(new_items) - len(old_items),
        d_paid=new_paid - old_paid,
        d_approved=new_app - old_app,
        d_agreements=new_agr - old_agr,
        d_position=(new_pos - old_pos)
        if (old_pos is not None and new_pos is not None)
        else None,
    )
