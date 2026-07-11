"""Тесты модели вероятности."""

from __future__ import annotations

import itertools
from datetime import date

from app.metrics import (
    CONTRACT_PARAMS,
    AnalysisContext,
    Horizon,
    _poisson_binomial_pmf,
    _prob_admission,
    analyze,
    calibrate,
    diff_snapshots,
    prior_enroll,
)
from tests.fixtures import T0, hours_ago, item

HORIZON = Horizon(enroll=date(2026, 8, 20), apply=date(2026, 8, 10))


def _ctx(places: int = 50, financing: str = "contract") -> AnalysisContext:
    return AnalysisContext(places=places, horizon=HORIZON, financing=financing)


def _brute_force_cdf(ps: list[float], k_max: int) -> float:
    """P(X <= k_max) полным перебором исходов."""
    total = 0.0
    for outcome in itertools.product([0, 1], repeat=len(ps)):
        if sum(outcome) <= k_max:
            prob = 1.0
            for happened, p in zip(outcome, ps, strict=True):
                prob *= p if happened else 1.0 - p
            total += prob
    return total


def test_poisson_binomial_matches_brute_force() -> None:
    """Точное ДП совпадает с полным перебором."""
    ps = [0.3, 0.7, 0.5, 0.9]
    places = 2
    expected = _brute_force_cdf(ps, places - 1)
    actual = _prob_admission(ps, places, 0.0)
    assert abs(actual - expected) < 1e-12


def test_pmf_overflow_bucket() -> None:
    """Последняя ячейка накапливает P(X >= cap)."""
    pmf = _poisson_binomial_pmf([0.5, 0.5, 0.5], cap=2)
    assert abs(sum(pmf) - 1.0) < 1e-12
    assert abs(pmf[2] - 0.5) < 1e-12  # P(X>=2) = 3/8 + 1/8


def test_probability_clamped_and_ordered() -> None:
    """Вероятность в [0, 1], сценарии упорядочены: pess <= base <= opt."""
    items = [item(i, str(100 + i), prio=1 + i % 5) for i in range(1, 40)]
    me = item(40, "999", ts=200.0)
    history = [(hours_ago(30), [*items[:30], me]), (T0, [*items, me])]
    a = analyze(history, "999", _ctx(places=10))
    assert 0.0 <= a.p_base <= 1.0
    assert a.p_pess <= a.p_base + 1e-9
    assert a.p_base <= a.p_opt + 1e-9


def test_quota_not_competitors_and_dup_prefers_general() -> None:
    """Квотники не конкуренты; дубль «квота+общий» решается в пользу общего."""
    items = [
        item(1, "1", paid=True),
        item(2, "42", q=True),  # тот же человек в квоте
        item(3, "3"),
        item(4, "42", ts=240.0),  # и в общем конкурсе
    ]
    a = analyze([(T0, items)], "42", _ctx(places=10))
    assert a.position == 4  # взята строка общего конкурса
    ahead = a.ahead
    assert ahead.paid + ahead.approved + ahead.agreement + ahead.none == 2


def test_paid_user_is_admitted_state() -> None:
    """Оплаченный договор фиксируется в my_state."""
    items = [item(1, "7", paid=True)]
    a = analyze([(T0, items)], "7", _ctx())
    assert a.my_state == "paid"


def test_withdrawn_counted() -> None:
    """Исчезнувшие из списка считаются, в том числе выше вас."""
    old = [item(1, "1"), item(2, "2"), item(3, "300")]
    new = [item(1, "2"), item(2, "300")]
    delta = diff_snapshots((hours_ago(6), old), (T0, new), "300")
    assert delta.withdrawn == 1
    assert delta.withdrawn_ahead == 1
    assert delta.d_position == -1


def test_calibration_short_window_respects_prior() -> None:
    """Короткое окно без конверсий не обнуляет приор (нижний зажим)."""
    cohort = [item(i, str(i), agr=True) for i in range(1, 60)]
    history = [(hours_ago(24), cohort), (T0, cohort)]
    calib = calibrate(history, hours_ago(-24 * 30), CONTRACT_PARAMS)
    assert calib.enough_data
    assert calib.odds_factor["agreement"] >= 0.2


def test_effective_priority_changes_prior() -> None:
    """Кросс-приоритет заменяет заявленный для неподписавших."""
    competitor = item(1, "5", prio=1)
    assert (
        prior_enroll(competitor, CONTRACT_PARAMS) == (CONTRACT_PARAMS.p_by_priority[1])
    )
    assert (
        prior_enroll(competitor, CONTRACT_PARAMS, eff_prio=3)
        == (CONTRACT_PARAMS.p_by_priority[3])
    )
    paid = item(1, "5", prio=1, paid=True)
    assert prior_enroll(paid, CONTRACT_PARAMS, eff_prio=3) == CONTRACT_PARAMS.p_paid


def test_safe_boundary_reported() -> None:
    """Безопасная граница ставится там, где P падает ниже 95%."""
    items = [item(i, str(i), agr=True) for i in range(1, 101)]
    me = item(101, "999", ts=100.0)
    a = analyze([(T0, [*items, me])], "999", _ctx(places=20))
    assert not a.safe_all
    assert a.safe_position is not None
    assert a.safe_position < 101
