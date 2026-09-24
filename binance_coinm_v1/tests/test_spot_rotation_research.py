"""BTC-quoted spot research: closed-bar decisions, risk budget, and cash conservation."""
from copy import deepcopy

import numpy as np
import pytest

from binance_coinm_v1.backtest.spot_rotation_research import (
    PERIOD, quantity_for_budget, simulate, timestamp, weekly_choices)

from .synth import bars_from


SPEC = {"filters": [
    {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001", "maxQty": "10000"},
    {"filterType": "NOTIONAL", "minNotional": "0.0001"},
]}


def test_weekly_momentum_uses_only_prior_closed_bars():
    n = 600
    c = np.linspace(.01, .02, n)
    market = {"ETHBTC": bars_from([(p,p*1.001,p*.999,p) for p in c], PERIOD,
                                   t0=timestamp("2024-01-01"))}
    original = weekly_choices(market, 20)
    assert original
    cutoff = sorted(original)[2]
    market["ETHBTC"].c[cutoff:] *= 100
    altered = weekly_choices(market, 20)
    assert {i:x for i,x in altered.items() if i <= cutoff} == {
        i:x for i,x in original.items() if i <= cutoff}


@pytest.mark.parametrize("factor", [1, 2])
def test_quantity_caps_fee_inclusive_stop_risk_and_initial_allocation(factor):
    fee, slip, stop_slip = .001 * factor, .0003 * factor, .0005 * factor
    qty, price, stop = quantity_for_budget(.02, .007, SPEC, fee, slip, stop_slip)
    assert qty > 0
    cost = qty * price * (1 + fee)
    exit_proceeds = qty * stop * (1 - stop_slip) * (1 - fee)
    assert cost <= .007 * .1
    assert cost - exit_proceeds <= .007 * .005


def test_minimum_order_is_skipped_instead_of_rounded_up():
    qty, _, _ = quantity_for_budget(.02, .00001, SPEC, .001, .0003, .0005)
    assert qty == 0


def test_stop_tick_rounding_is_included_in_risk_budget():
    spec = deepcopy(SPEC)
    spec["filters"].append({"filterType": "PRICE_FILTER", "tickSize": "0.00001"})
    qty, price, stop = quantity_for_budget(.022873, .007, spec, .001, .0003, .0005)
    assert stop == pytest.approx(.02172)
    assert qty * (price * 1.001 - stop * .9995 * .999) <= .007 * .005


def test_entry_bar_stop_charges_both_fees_and_conserves_btc():
    bars = bars_from([(.02,.021,.018,.019),(.019,.02,.018,.019)], PERIOD, t0=0)
    result = simulate({"ETHBTC":bars}, {"ETHBTC":SPEC}, {0:"ETHBTC"}, 0, 2 * PERIOD)
    assert result["n"] == 1
    trade = result["trades"][0]
    assert trade["reason"] == "stop"
    assert 0 < -trade["net_btc"] <= .007 * .005
    assert trade["fee_btc"] > 0
    assert result["final_btc"] == pytest.approx(.007 + trade["net_btc"])


def test_gap_can_exceed_planned_risk_and_future_prices_are_excluded():
    bars = bars_from([(.02,.021,.0195,.02),(.015,.016,.014,.015),(.02,.02,.02,.02)], PERIOD, t0=0)
    market = {"ETHBTC":bars}
    result = simulate(market, {"ETHBTC":SPEC}, {0:"ETHBTC"}, 0, 2 * PERIOD)
    assert result["trades"][0]["reason"] == "gap_stop"
    assert -result["trades"][0]["net_btc"] > .007 * .005
    bars.o[2:] *= 100
    bars.c[2:] *= 100
    assert simulate(market, {"ETHBTC":SPEC}, {0:"ETHBTC"}, 0, 2 * PERIOD) == result


def test_no_positive_momentum_keeps_btc_units_constant():
    p = np.linspace(.02,.01,200)
    bars = bars_from([(x,x*1.01,x*.99,x) for x in p], PERIOD,
                     t0=timestamp("2024-01-01"))
    choices = weekly_choices({"ETHBTC":bars},20)
    assert choices and all(x is None for x in choices.values())
    result = simulate({"ETHBTC":bars},{"ETHBTC":SPEC},choices,float(bars.t[0]),float(bars.t[-1])+PERIOD)
    assert result["btc_return"] == 0
    assert result["n"] == 0
