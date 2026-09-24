"""Common-BTC accounting invariants for public-data multiasset exploration."""

from types import SimpleNamespace

import numpy as np
import pytest

from binance_coinm_v1.backtest.multiasset_research import (
    aligned_prices, btc_curve, collateral_for_btc, describe,
)
from binance_coinm_v1.strategy.price_action import Bars


def bars(prices):
    p = np.asarray(prices, dtype=float)
    return Bars(np.arange(len(p)) * 3600.0, p, p, p, p, np.ones(len(p)), 3600)


def test_positive_altcoin_pnl_can_still_lose_btc():
    # 10% more ETH is not a BTC profit if ETH/BTC halves.
    initial = collateral_for_btc(.007, 60000, 3000, .002)
    out = btc_curve([initial, initial * 1.1], [3000, 1500], [60000, 60000], .002)
    assert out[-1] < .007
    assert out[-1] / .007 == pytest.approx(1.1 * .5 * .998 ** 2)


def test_btc_collateral_conversion_is_identity():
    assert collateral_for_btc(.007, 60000, 60000, 0) == pytest.approx(.007)
    np.testing.assert_allclose(btc_curve([.007, .008], [60000, 80000],
                                        [60000, 80000], 0), [.007, .008])


def test_collateral_is_native_float_for_decimal_sizing_bridge():
    value = collateral_for_btc(.007, np.float64(60000), np.float64(3000), .002)
    assert type(value) is float


def test_conversion_cost_charged_at_both_ends_for_passive_hold():
    coin = collateral_for_btc(.007, 60000, 3000, .002)
    value = btc_curve([coin], [3000], [60000], .002)[0]
    assert value == pytest.approx(.007 * .998 ** 2)


def test_currency_scale_changes_collateral_not_btc_value():
    one = collateral_for_btc(.007, 60000, 3000, .002)
    two = collateral_for_btc(.007, 60000, 3, .002)
    assert two == pytest.approx(one * 1000)
    assert btc_curve([one], [3600], [62000], .002)[0] == pytest.approx(
        btc_curve([two], [3.6], [62000], .002)[0])


def test_alignment_rejects_missing_timestamp_and_invalid_price():
    b = bars([100, 101])
    with pytest.raises(ValueError, match="matching"):
        aligned_prices(b, [1800])
    with pytest.raises(ValueError, match="matching"):
        aligned_prices(b, [7200])
    with pytest.raises(ValueError, match="Invalid"):
        aligned_prices(bars([0]), [0])


@pytest.mark.parametrize("value", [-1, 1, float("nan")])
def test_invalid_conversion_cost_rejected(value):
    with pytest.raises(ValueError, match="Conversion cost"):
        collateral_for_btc(.007, 60000, 3000, value)
    with pytest.raises(ValueError, match="Conversion cost"):
        btc_curve([1], [3000], [60000], value)


def test_report_distinguishes_collateral_gain_from_btc_wallet_loss():
    ds = SimpleNamespace(mark=bars([3000, 1500]), ltf=bars([3000, 1500]),
                         spec=SimpleNamespace(margin_asset="ETH"))
    btc_ds = SimpleNamespace(mark=bars([60000, 60000]))
    res = SimpleNamespace(config=SimpleNamespace(start_equity_btc=.14),
                          equity_ts=np.array([0, 3600]), equity_curve=np.array([.14, .154]),
                          final_equity=.154, funding_total=0,
                          trades=[{"net_btc": .014, "fee_btc": 0}], skipped=[])
    report = describe(ds, btc_ds, res, .007, 0)
    assert report["margin_asset"] == "ETH"
    assert report["collateral_trading_return"] == pytest.approx(.1)
    assert report["btc_total_return"] == pytest.approx(-.45)
    assert report["passive_same_asset_btc_return"] == pytest.approx(-.5)
    assert report["excess_vs_same_asset_btc_percentage_points"] == pytest.approx(5)
    assert report["btc_max_drawdown"] == pytest.approx(.45)
    assert "fees_btc" not in report
    assert "final_equity_btc" not in report


def test_invalid_balance_and_unmatched_shapes_rejected():
    with pytest.raises(ValueError, match="Non-finite"):
        btc_curve([float("nan")], [3000], [60000], 0)
    with pytest.raises(ValueError, match="align"):
        btc_curve([1, 2], [3000], [60000], 0)
