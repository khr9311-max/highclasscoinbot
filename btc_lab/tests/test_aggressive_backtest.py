"""Signal parity, inverse accounting and sleeve transfers of the aggressive replay."""
from decimal import Decimal as D

import numpy as np
import pytest

from btc_lab import aggressive_backtest as ab
from btc_portfolio.signals import alt_signal, coinm_signal, ema


def test_window_ema_matches_the_live_seeded_ema():
    closes = 50000 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 260)))
    fast, slow = ab.window_ema(closes, 20), ab.window_ema(closes, 80)
    for i in (200, 231, 259):
        window = [D(str(x)) for x in closes[i - 200:i]]
        assert fast[i] == pytest.approx(float(ema(window, 20)), rel=1e-9)
        assert slow[i] == pytest.approx(float(ema(window, 80)), rel=1e-9)


def test_direction_matches_coinm_signal():
    rng = np.random.default_rng(2)
    closes = 30000 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    period, t0 = 14_400_000, 1_700_000_000_000 // 14_400_000 * 14_400_000
    data = object.__new__(ab.Data)
    data.fast, data.slow = ab.window_ema(closes, 20), ab.window_ema(closes, 80)
    for i in (210, 250, 299):
        rows = [[t0 + k * period, str(c), str(c * 1.01), str(c * 0.99), str(c), "0", t0 + (k + 1) * period - 1]
                for k, c in enumerate(closes[:i])]
        live = coinm_signal({"klines": rows, "server_time_ms": t0 + i * period + 5})
        assert data.direction(i) == live["direction"]


def test_alt_winner_matches_alt_signal():
    rng = np.random.default_rng(3)
    data = object.__new__(ab.Data)
    data.dclose = {s: 0.01 * np.exp(np.cumsum(rng.normal(0.002, 0.03, 90))) for s in ab.ALTS}
    day_ms, t0 = 86_400_000, 1_700_006_400_000 // 86_400_000 * 86_400_000
    for day in (61, 75, 89):
        markets = {s: {"klines": [[t0 + k * day_ms, str(c), str(c), str(c), str(c), "0", t0 + (k + 1) * day_ms - 1]
                                  for k, c in enumerate(data.dclose[s][:day])],
                       "server_time_ms": t0 + day * day_ms + 5} for s in ab.ALTS}
        winner, scores = data.alt_winner(day)
        live = alt_signal(markets)
        assert winner == live["symbol"]
        for s in ab.ALTS:
            assert scores[s] == pytest.approx(float(D(live["scores"][s])), rel=1e-9)


def test_isolated_liquidation_prices():
    assert ab.liquidation_price(100.0, 1, 0.025) == pytest.approx(76.875)
    assert ab.liquidation_price(100.0, -1, 0.025) == pytest.approx(146.25)


def stub(n=4, alt_price=0.002, coin_price=100000.0):
    data = object.__new__(ab.Data)
    data.n, data.t0 = n, 0
    data.alt = {s: {"open": np.full(n, alt_price), "close": np.full(n, alt_price)} for s in ab.ALTS}
    data.coin = {"open": np.full(n, coin_price)}
    data.mark = {k: np.full(n, coin_price) for k in ("open", "high", "low")}
    data.funding = np.zeros(n)
    data.step = {s: 0.001 for s in ab.ALTS}
    data.min_notional = {s: 0.0001 for s in ab.ALTS}
    data.mmr = 0.025
    return data


def test_inverse_close_accounting():
    replay = ab.Replay(stub(), ab.Config(capital_btc=1.0, cost_mult=1.0))
    wallet = replay.coin_wallet
    replay.contracts, replay.entry = 3, 100000.0
    replay.close(0, 110000.0, "flip")
    pnl = 300 * (1 / 100000 - 1 / 110000)
    fee = 300 / 110000 * ab.FEE_COIN
    assert replay.coin_wallet == pytest.approx(wallet + pnl - fee)
    assert replay.contracts == 0 and replay.blocked is None


def test_monthly_transfer_only_moves_btc_between_sleeves():
    replay = ab.Replay(stub(), ab.Config(capital_btc=0.003, cost_mult=0.0))
    replay.spot_btc, replay.coin_wallet = 0.0005, 0.0025      # 17% spot, target 50%
    before = replay.total(0)
    replay.rebalance(0)
    assert replay.total(0) == pytest.approx(before, rel=1e-12)
    assert replay.spot_equity(0) / replay.total(0) == pytest.approx(0.5, abs=0.01)
    assert replay.n["transfers"] == 1


def test_stop_gap_rule_blocks_stops_too_close_to_liquidation():
    data = stub()
    data.vol = np.full(4, np.nan)
    replay = ab.Replay(data, ab.Config(capital_btc=1.0, spot_fraction=0.0, stop=0.20))
    replay.open(0, 0, 0, 1)                                   # liquidation near -23%: a 20% stop is refused
    assert replay.contracts == 0 and replay.n["rule_skips"] == 1
    replay = ab.Replay(data, ab.Config(capital_btc=1.0, spot_fraction=0.0, stop=0.12))
    replay.open(0, 0, 0, 1)
    assert replay.contracts == 2000                           # floor(2 x 1 BTC x 100,030 / 100)
    assert replay.stop_px == pytest.approx(100030 * 0.88)
