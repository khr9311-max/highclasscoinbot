"""Signal timing and BTC accounting of the multi-coin trend portfolio."""
import numpy as np
import pytest

from btc_lab import trend_portfolio as tp


def test_signal_at_a_bar_does_not_use_that_bar_close():
    rng = np.random.default_rng(2)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    base = tp.signals(close)
    changed = close.copy()
    changed[300:] *= 3.0                      # a huge move from bar 300 on
    moved = tp.signals(changed)
    assert np.array_equal(base[:301], moved[:301])
    assert not np.array_equal(base[301:], moved[301:])


def test_alt_leg_is_converted_to_btc_at_the_bar_close():
    n = 3
    d = {"open_ms": np.arange(n), "BTC_close": np.array([100.0, 110.0, 110.0]), "BTC_funding": np.zeros(n)}
    for s in tp.ALTS:
        d[f"{s}_close"] = np.array([10.0, 11.0, 12.1])
        d[f"{s}_funding"] = np.zeros(n)
    raw, _ = tp.bar_returns(d)
    # USD P&L of 10% on 100 USD notional, added to unchanged BTC collateral at the new BTC price
    assert raw["ETHUSDT"][1] == pytest.approx(0.10 * 100 / 110)
    assert raw["ETHUSDT"][2] == pytest.approx(0.10)                # alt +10%, BTC flat
    assert raw["BTC"][1] == pytest.approx(1 - 100 / 110)           # inverse long
