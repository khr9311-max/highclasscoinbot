"""Research validation must not borrow future fills or confuse BTC and USD wealth."""

import numpy as np
import pytest

from binance_coinm_v1.backtest.data import Dataset
from binance_coinm_v1.backtest.research import describe_result, selection_report, window
from binance_coinm_v1.backtest.simulator import SimConfig, SimResult

from .helpers import load_spec
from .synth import aggregate, bars_from
from .test_backtest import FakePre, T0, rows_flat, sig


def test_periods_reset_positions_and_cannot_use_next_period_open():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80200)
    for k in range(72, 80):
        rows[k] = (80200, 80500, 80100, 80400)
    bars = bars_from(rows, t0=T0)
    ds = Dataset("BTCUSD_PERP", bars, aggregate(bars, 4), bars, 0, [], load_spec())
    pre = FakePre(100, {(63, 1): sig(63, 1, 80000, 79000, [90000]),
                        (70, 1): sig(70, 1, 80000, 79000, [90000])})
    cfg = SimConfig(variant="zone", max_hold_bars=100, slippage_bps=10)
    start, end = T0 + 65 * 3600, T0 + 80 * 3600
    result = window(ds, pre, start, end).run(cfg)
    assert len(result.trades) == 1  # Earlier pending signal is not carried in.
    assert result.trades[0]["signal_ts"] == T0 + 71 * 3600
    assert result.trades[0]["exit_ts"] == end
    assert result.trades[0]["exits"][-1][2] < rows[79][3]  # Terminal slippage.
    bars.o[80:] *= 1.5
    bars.c[80:] *= 1.5
    future_changed = window(ds, pre, start, end).run(cfg)
    assert future_changed.trades == result.trades
    assert future_changed.final_equity == result.final_equity
    assert pre.valid[61:65].all()  # Original precompute was not mutated.


def test_btc_cash_is_not_usd_cash_and_first_drop_counts_in_drawdown():
    bars = bars_from([(100, 100, 90, 90), (90, 90, 80, 80)], t0=T0)
    ds = Dataset("BTCUSD_PERP", bars, bars, bars, 0, [], load_spec())
    result = SimResult(SimConfig(), [], [], np.ones(2), bars.t, 1.0, 0)
    s = describe_result(ds, result, T0, T0 + 7200)
    assert s["total_return"] == 0
    assert s["usd_total_return_mark_proxy"] == pytest.approx(-0.2)
    assert s["usd_max_drawdown_mark_proxy"] == pytest.approx(0.2)
    assert s["btc_hold_usd_return"] == pytest.approx(-0.2)
    assert s["usd_excess_vs_btc_hold_percentage_points"] == 0


def score(ret, n=50):
    return dict(n=n, total_return=ret, cagr_btc=ret, max_drawdown=0.1)


def candidate(dev, val, stress, recent):
    return {"account": {"development": {"base": score(dev)},
                        "validation": {"base": score(val), "stress_2x": score(stress)},
                        "recent": {"base": score(recent)}}}


def test_failed_development_winner_is_not_replaced_by_recent_winner():
    rows = {"train_best": candidate(.2, -.01, -.02, -.5),
            "recent_best": candidate(.1, .1, .05, 2.0)}
    result = selection_report(rows)
    assert result["development_winner"] == "train_best"
    assert not result["passed_validation_screen"]
    assert not result["live_eligible"]
    rows["recent_best"]["account"]["recent"]["base"] = score(-2.0)
    assert selection_report(rows) == result


def test_cost_stress_and_small_samples_block_promotion():
    rows = {"only": candidate(.2, .1, -.01, .5)}
    assert not selection_report(rows)["passed_validation_screen"]
    rows["only"]["account"]["validation"]["stress_2x"] = score(.1, n=29)
    assert not selection_report(rows)["passed_validation_screen"]
    rows["only"]["account"]["development"]["base"] = score(.2, n=1)
    assert selection_report(rows)["development_winner"] is None
