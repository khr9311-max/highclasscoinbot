from datetime import datetime, timezone
import math

import pytest

from btc_lab.growth_metrics import growth_metrics


def stamp(date):
    return datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()


def result(*points):
    return {"equity_curve": [{"t": stamp(t) if isinstance(t, str) else t, "equity_btc": equity}
                             for t, equity in points]}


def test_month_end_attribution_and_partial_month_compound_from_start():
    metrics = growth_metrics(result(("2020-01-01", 100), ("2020-02-01", 110),
                                    ("2020-03-01", 99), ("2020-03-16", 108.9)))
    assert metrics["monthly_returns_pct"] == pytest.approx({"2020-01": 10, "2020-02": -10, "2020-03": 10})
    assert metrics["monthly_compounded_return_pct"]["2020-03"] == pytest.approx(8.9)
    compounded = math.prod(1 + value / 100 for value in metrics["monthly_returns_pct"].values())
    assert compounded == pytest.approx(108.9 / 100)
    assert metrics["partial_months"] == ["2020-03"]
    assert metrics["complete_month_ends"] == ["2020-01", "2020-02"]
    assert metrics["terminal_equity_btc"] == 108.9


def test_recovery_uses_elapsed_days_and_tied_peak_resets_clock():
    day = 86_400
    metrics = growth_metrics(result((0, 100), (day, 90), (3 * day, 100),
                                    (4 * day, 100), (5 * day, 90), (8 * day, 101),
                                    (9 * day, 98)))
    assert metrics["recovered_drawdown_episodes"] == 2
    assert metrics["max_underwater_days"] == 4
    assert metrics["current_underwater_days"] == 1
    assert metrics["current_drawdown_pct"] == pytest.approx((1 - 98 / 101) * 100)
    assert metrics["max_drawdown_pct"] == pytest.approx(10)


def test_initial_loss_and_unrecovered_episode_include_initial_balance():
    metrics = growth_metrics(result((0, 100), (3600, 80), (10 * 86_400, 90)))
    assert metrics["recovered_drawdown_episodes"] == 0
    assert metrics["max_underwater_days"] == 10
    assert metrics["current_underwater_days"] == 10
    assert metrics["max_drawdown_pct"] == pytest.approx(20)
    assert metrics["current_drawdown_pct"] == pytest.approx(10)


def test_partial_first_month_does_not_create_a_phantom_previous_month():
    metrics = growth_metrics(result(("2020-01-15", 100), ("2020-02-01", 110),
                                    ("2020-03-01", 120)))
    assert list(metrics["monthly_returns_pct"]) == ["2020-01", "2020-02"]
    assert metrics["partial_months"] == ["2020-01"]
    assert metrics["rolling_12m_return_pct"] == {}
    assert metrics["rolling_12m_positive_fraction"] is None


def test_calendar_12_month_windows_include_initial_boundary_but_exclude_last_partial():
    points = [("2020-01-01", 100)]
    for number in range(1, 25):
        year = 2020 + number // 12
        month = number % 12 + 1
        points.append((f"{year:04d}-{month:02d}-01", 100 * 1.01 ** number))
    points.append(("2022-01-15", 5000))
    metrics = growth_metrics(result(*points))
    rolling = metrics["rolling_12m_return_pct"]
    assert len(rolling) == 13
    assert list(rolling)[0] == "2020-12"
    assert list(rolling)[-1] == "2021-12"
    assert "2022-01" not in rolling
    assert all(value == pytest.approx((1.01 ** 12 - 1) * 100) for value in rolling.values())
    assert metrics["rolling_12m_positive_fraction"] == 1
    assert metrics["worst_12m_return_pct"] == pytest.approx((1.01 ** 12 - 1) * 100)


def test_12_month_window_requires_exact_prior_calendar_anchor():
    points = [("2020-01-15", 100)]
    for number in range(1, 14):
        year, month = 2020 + number // 12, number % 12 + 1
        points.append((f"{year:04d}-{month:02d}-01", 100 - number))
    metrics = growth_metrics(result(*points))
    assert list(metrics["rolling_12m_return_pct"]) == ["2021-01"]
    assert metrics["rolling_12m_return_pct"]["2021-01"] == pytest.approx((87 / 99 - 1) * 100)
    assert metrics["rolling_12m_positive_fraction"] == 0


def test_nonpositive_wallet_preserves_ruin_without_fictitious_positive_growth():
    metrics = growth_metrics(result(("2020-01-01", 100), ("2020-02-01", 50),
                                    ("2020-03-01", -10), ("2020-04-01", -10)))
    assert metrics["monthly_returns_pct"]["2020-01"] == -50
    assert metrics["monthly_returns_pct"]["2020-02"] == -120
    assert metrics["monthly_returns_pct"]["2020-03"] is None
    assert metrics["nonpositive_equity_observed"]
    assert metrics["max_drawdown_pct"] == pytest.approx(110)


def test_validation_rejects_nonfinite_and_nonchronological_curves():
    bad_inputs = [result(), result((0, 0)), result((0, 1), (0, 2)),
                  result((1, 1), (0, 2)), result((0, 1), (3600, math.inf)),
                  result((math.nan, 1)), {"equity_curve": None},
                  {"equity_curve": [{"t": 0, "equity_btc": "1"}]}]
    for bad in bad_inputs:
        with pytest.raises(ValueError):
            growth_metrics(bad)


def test_initial_point_alone_has_no_monthly_return_or_drawdown():
    metrics = growth_metrics(result(("2020-01-01", 1)))
    assert metrics["monthly_returns_pct"] == {}
    assert metrics["current_underwater_days"] == 0
    assert metrics["max_underwater_days"] == 0
    assert metrics["recovered_drawdown_episodes"] == 0
    assert metrics["cumulative_return_pct"] == 0
