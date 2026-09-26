"""Inverse-contract liquidation prices used by the leverage study."""
import math

import pytest

from btc_lab import leverage_study as ls


def test_long_liquidates_before_the_naive_one_over_leverage_move():
    p = ls.liquidation_price(100_000, 1, 10, 0.001)       # 10 x $100 on 0.001 BTC at $100k = 10x
    assert p == pytest.approx(1.004 * 1000 / (0.001 + 0.01))
    assert 0.08 < 1 - p / 100_000 < 0.10                  # BTC collateral falls with the price


def test_short_liquidation_and_unleveraged_short_never_liquidates():
    p = ls.liquidation_price(100_000, -1, 10, 0.001)
    assert p == pytest.approx(0.996 * 1000 / (0.01 - 0.001))
    assert ls.liquidation_price(100_000, -1, 1, 0.001) == math.inf
