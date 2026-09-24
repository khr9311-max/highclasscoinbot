"""The historical replay must decide exactly as btc_portfolio does."""
from decimal import Decimal as D
import math
import random

import pytest

from btc_lab import portfolio_backtest as bt
from btc_portfolio.signals import alt_signal, coinm_signal

T0 = 1_700_006_400_000 // bt.DAY * bt.DAY


def kline(opening, period, o, h, low, c):
    return [opening, str(o), str(h), str(low), str(c), "0", opening + period - 1]


def fake_data(alts=("ETHBTC", "SOLBTC"), days=70, hours=None, alt_prices=None, coin_price=50000.0, seed=1):
    rng = random.Random(seed)
    data = object.__new__(bt.Data)
    data.alts = tuple(alts)
    data.alt_d, data.alt_h = {}, {}
    for s in data.alts:
        price, daily = 0.01, {}
        for k in range(days):
            price *= 1 + rng.uniform(-.05, .05)
            daily[T0 + k * bt.DAY] = (price, price * 1.01, price * .99, price)
        data.alt_d[s] = daily
        data.alt_h[s] = {}
    hours = hours or []
    for s in data.alts:
        for t in hours:
            p = (alt_prices or {}).get((s, t), 0.01)
            data.alt_h[s][t] = p if isinstance(p, tuple) else (p, p, p, p)
    start = T0 - 300 * bt.H4
    data.c4h, price = {}, coin_price
    for k in range(300 + days * 6):
        price *= 1 + rng.uniform(-.02, .02)
        data.c4h[start + k * bt.H4] = (price, price * 1.01, price * .99, price)
    data.c1h = {t: (coin_price,) * 4 for t in hours}
    data.m1h = {t: (coin_price,) * 4 for t in hours}
    data.funding = {}
    data.step = {s: 0.001 for s in data.alts}
    data.min_notional = {s: 0.0001 for s in data.alts}
    data._coin, data._alt = {}, {}
    return data


def test_coin_signal_matches_live_engine():
    data = fake_data()
    for t in (T0 + 3 * bt.H4 + 123, T0 + 40 * bt.H4, T0 + 55 * bt.DAY + 7):
        end = t // bt.H4 * bt.H4
        rows = [kline(o, bt.H4, *v) for o, v in sorted(data.c4h.items()) if o < end]
        live = coinm_signal({"klines": rows, "server_time_ms": t})
        bar, direction, stop = data.coin_signal(t)
        assert str(bar) == live["bar"] and direction == live["direction"]
        assert stop == pytest.approx(float(D(live["stop_fraction"])), rel=1e-12)


def test_alt_signal_matches_live_engine():
    data = fake_data(alts=bt.ALTS)
    for t in (T0 + 61 * bt.DAY + 5, T0 + 66 * bt.DAY):
        markets = {s: {"klines": [kline(o, bt.DAY, *v) for o, v in sorted(data.alt_d[s].items()) if o < t // bt.DAY * bt.DAY],
                       "server_time_ms": t} for s in bt.ALTS}
        live = alt_signal(markets)
        bar, symbol, scores = data.alt_signal(t)
        assert str(bar) == live["bar"] and symbol == live["symbol"]
        for s in bt.ALTS:
            assert scores[s] == pytest.approx(float(D(live["scores"][s])), rel=1e-12)


def test_alt_stop_blocks_same_day_reentry_then_reenters_next_day():
    day = T0 + 62 * bt.DAY
    hours = [day + k * bt.HOUR for k in range(30)]
    winner = "SOLBTC"
    prices = {(winner, hours[0]): 0.01, (winner, hours[1]): (0.01, 0.01, 0.0094, 0.0095)}
    for t in hours[2:]:
        prices[(winner, t)] = 0.0095
    data = fake_data(hours=hours, alt_prices=prices)
    for s, slope in (("ETHBTC", .001), ("SOLBTC", .003)):  # both rise in BTC, SOL faster
        data.alt_d[s] = {o: (0.01 * (1 + slope) ** k,) * 4 for k, o in enumerate(sorted(data.alt_d[s]))}
    assert data.alt_signal(day)[1] == data.alt_signal(day + bt.DAY)[1] == winner
    replay = bt.Portfolio(data, enable_coin=False).run(hours[0], hours[-1] + bt.HOUR)
    assert [x["reason"] for x in replay.trades] == ["stop"]
    assert replay.trades[0]["exit"] == pytest.approx(0.01 * 1.0003 * .95 * .9997)
    assert replay.stats["alt_entries"] == 2  # stop on day one, re-entry at the next UTC day only
    assert replay.alt_entry[0] == winner


def test_minimum_contract_above_risk_budget_is_skipped():
    hours = [T0 + 62 * bt.DAY + k * bt.HOUR for k in range(8)]
    replay = bt.Portfolio(fake_data(hours=hours, coin_price=20000.0), enable_alt=False).run(hours[0], hours[-1] + bt.HOUR)
    assert replay.stats["coin_entries"] == 0 and replay.stats["coin_min_contract_skips"] == 2
    assert replay.coin_wallet == 0.0018


def test_inverse_contract_accounting_conserves_btc():
    hours = [T0 + 62 * bt.DAY + k * bt.HOUR for k in range(2)]
    replay = bt.Portfolio(fake_data(hours=hours), enable_alt=False, fractional=True)
    replay.day_start = (hours[0] // bt.DAY, 0.003)
    replay.coin_enter(hours[0], 1, 0.02, 0.003)
    qty, entry = replay.coin_qty, replay.coin_entry
    replay.coin_close(hours[1], entry * 1.1, "reverse")
    gross = qty * 100 * (1 / entry - 1 / (entry * 1.1))
    fees = qty * 100 / entry * .0005 + qty * 100 / (entry * 1.1) * .0005
    assert replay.coin_wallet == pytest.approx(0.0018 + gross - fees, abs=1e-15)
    assert replay.stats["fees_btc"] == pytest.approx(fees)
    assert math.isclose(replay.trades[0]["pnl_btc"], gross - qty * 100 / (entry * 1.1) * .0005)
