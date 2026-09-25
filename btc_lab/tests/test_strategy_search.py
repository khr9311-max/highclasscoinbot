"""Causality and accounting checks for the pre-registered strategy search."""
import numpy as np
import pytest

from btc_lab import strategy_search as ss


def stub_market(days=12, seed=3):
    rng = np.random.default_rng(seed)
    market = object.__new__(ss.Market)
    market.t0 = 1_700_006_400_000 // 86_400_000 * 86_400_000
    market.n = days * ss.DAY_BARS
    market.series = {}
    for name in (*ss.ALTS, "BTCUSD_PERP"):
        base = 50000.0 if name == "BTCUSD_PERP" else 0.01
        close = base * np.exp(np.cumsum(rng.normal(0, 0.002, market.n)))
        open_ = np.concatenate(([base], close[:-1]))
        wiggle = np.abs(rng.normal(0, 0.001, market.n)) * close
        market.series[name] = {"open": open_, "close": close, "high": np.maximum(open_, close) + wiggle,
                               "low": np.minimum(open_, close) - wiggle, "volume": rng.uniform(1, 3, market.n)}
    market.funding = np.zeros(market.n)
    market._bars = {}
    return market


def test_prior_exposes_only_completed_values():
    assert np.isnan(ss.prior(np.array([1.0, 2.0, 3.0]))[0])
    assert list(ss.prior(np.array([1.0, 2.0, 3.0]))[1:]) == [1.0, 2.0]


def test_regime_uses_only_closed_higher_timeframe_bars():
    market = stub_market()
    bull, _ = ss.regime(market, "ETHBTC", "5m", "1h")
    hourly = market.bars("ETHBTC", "1h")["close"]
    fast, slow = ss.ema(hourly, 20), ss.ema(hourly, 50)
    expected = (fast > slow) & (hourly > slow)
    expected[:50] = False
    for i in (12 * 60 + 5, 12 * 100, 12 * 150 + 11):
        assert bull[i] == expected[i // 12 - 1]  # the hour that closed before this 5m bar opened


def test_breakout_past_trades_do_not_change_when_the_future_changes():
    market = stub_market()
    cand = ss.Candidate("breakout", "coinm", dict(tf="5m", lookback=12, filters="trend", exit="tp2_ema"))
    before = ss.breakout(market, "BTCUSD_PERP", cand, 1, 1.0)
    assert len(before.ret) > 3
    cut = int(before.exit_bar[len(before.ret) // 2]) + 1
    altered = stub_market()
    for key in ("open", "high", "low", "close"):
        altered.series["BTCUSD_PERP"][key][cut:] *= 1.3
    after = ss.breakout(altered, "BTCUSD_PERP", cand, 1, 1.0)
    keep = before.exit_bar < cut
    assert np.array_equal(before.entry_bar[keep], after.entry_bar[:keep.sum()])
    assert np.allclose(before.ret[keep], after.ret[:keep.sum()])


def test_stop_is_assumed_before_target_in_the_same_bar():
    bars = {"open": np.array([100.0, 100, 100, 100]), "high": np.array([100.0, 110, 100, 100]),
            "low": np.array([100.0, 90, 100, 100]), "close": np.array([100.0, 100, 100, 100]),
            "funding": np.zeros(4), "k": 1}
    signal = np.array([True, False, False, False])
    tr = ss.run_events("BTCUSD_PERP", bars, 1, signal, bars["open"], np.full(4, 0.05), tp_mult=1,
                       stop_in_entry_bar=False, max_hold=3, cost_mult=0.0)
    assert tr.ret[0] == pytest.approx(1 - 100 / 95)


def test_inverse_and_spot_returns_are_in_btc():
    assert ss.trade_return("BTCUSD_PERP", 1, 100.0, 110.0, 0.0, 0.0) == pytest.approx(1 - 100 / 110)
    assert ss.trade_return("BTCUSD_PERP", -1, 100.0, 90.0, 0.0, 0.0) == pytest.approx(100 / 90 - 1)
    assert ss.trade_return("ETHBTC", 1, 0.05, 0.055, 0.0, 0.0) == pytest.approx(0.1)
    # one round trip of spot costs: two 0.1% fees, two 3 bp slips, two half ticks
    cost = -ss.trade_return("ETHBTC", 1, 0.05, 0.05, 0.0, 1.0)
    assert cost == pytest.approx(0.0027, abs=0.0002)
