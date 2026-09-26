"""Fill rules of the limit-order follow-up."""
import numpy as np
import pytest

from btc_lab import maker_fill as mf


def run(close, high, low, target_next, patience=2):
    n = len(close)
    return mf.simulate(np.array(close, float), np.array(high, float), np.array(low, float), np.zeros(n),
                       np.array(target_next, float), patience, maker_fee=0.0002)


def test_buy_needs_a_trade_through_by_one_tick():
    # order at close 100 after bar 0; bar 1 touches 100 exactly (no fill), bar 2 trades through
    close = [100, 100.5, 101, 102, 102]
    low = [100, 100, 99.8, 101, 101]
    r, pos, tr, stats = run(close, close, low, [1, 1, 1, 1, 1], patience=5)
    assert list(pos) == [0, 0, 1, 1, 1]
    assert stats["maker_fills"] == 1
    assert r[2] == pytest.approx((1 - 100 / 101) - 0.0002)


def test_unfilled_exit_goes_taker_after_patience():
    close = [100, 100, 100, 100, 100, 100, 100]
    low = [100, 99, 100, 100, 100, 100, 100]      # entry fills in bar 1
    high = close                                   # sell limit never trades through
    target = [1, 0, 0, 0, 0, 0, 0]                 # exit wanted from the close of bar 1
    r, pos, tr, stats = run(close, high, low, target, patience=2)
    assert stats["maker_fills"] == 1 and stats["taker_exits"] == 1
    assert list(pos) == [0, 1, 1, 0, 0, 0, 0]      # posted at close 1, aged two bars, taker at close 3
    assert len(tr.ret) == 1 and tr.ret[0] < -0.0002


def test_unfilled_entry_is_reposted_not_forced():
    close = [100, 101, 102, 103, 104, 105]
    low = [100, 100.5, 101.5, 102.5, 103.5, 104.5]   # price runs away: buy limits never fill
    r, pos, tr, stats = run(close, close, low, [1] * 6, patience=2)
    assert not pos.any()
    assert stats["reposts"] >= 1 and stats["taker_exits"] == 0
    assert np.all(r == 0)
