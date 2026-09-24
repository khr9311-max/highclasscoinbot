from dataclasses import replace
import math
import random

import pytest

from btc_lab.engine import Bar, Config, Funding, Spec, run


def bar(t=0, price=50_000, *, high=None, low=None, close=None, mark=None):
    high, low, close = high or price, low or price, close or price
    mark = mark or price
    return Bar(t, price, high, low, close, mark, high, low, close)


ZERO_COST = Config(initial_btc=1.0, fee=0, slip_bps=0, stop_slip_bps=0,
                   max_exposure=1, stop_pct=0.9, liquidation_fee=0)


def test_flat_cash_including_initial_balance():
    result = run([bar(), bar(3600, 49_000)], [], {}, cfg=ZERO_COST)
    assert result["summary"]["final_btc"] == 1
    assert result["summary"]["trade_count"] == 0
    assert result["summary"]["max_drawdown_pct"] == 0
    assert len(result["equity_curve"]) == 3


@pytest.mark.parametrize("direction", [1, -1])
def test_inverse_profit_and_loss_and_ledger(direction):
    result = run([bar(), bar(3600, 55_000)], [], {0: direction}, cfg=ZERO_COST)
    expected = direction * 500 * 100 * (1 / 50_000 - 1 / 55_000)
    assert result["summary"]["realized_btc"] == pytest.approx(expected)
    assert result["summary"]["final_btc"] == pytest.approx(1 + expected)
    assert sum(x["net_btc"] for x in result["trades"]) == pytest.approx(expected)


def test_open_funding_applies_only_to_prior_position_and_sign():
    funding = [Funding(0, .01, 50_000), Funding(3600, .01, 50_000)]
    for direction in [1, -1]:
        result = run([bar(), bar(3600)], funding, {0: direction, 3600: 0}, cfg=ZERO_COST)
        assert result["summary"]["funding_btc"] == pytest.approx(-direction * .01)
        assert len([x for x in result["events"] if x["type"] == "funding"]) == 1


def test_intrabar_funding_and_missing_mark_are_audited():
    result = run([bar()], [Funding(1, .01)], {0: 1}, cfg=ZERO_COST)
    assert result["summary"]["funding_btc"] == pytest.approx(-.01)
    assert result["audit"]["intrabar_funding"] == 1
    assert result["audit"]["missing_funding_marks"] == 1


def test_adverse_funding_omits_short_credit_when_same_bar_stops():
    bars = [bar(high=56_000)]
    funding = [Funding(1800, .01, 50_000)]
    cfg = replace(ZERO_COST, stop_pct=.1)
    default = run(bars, funding, {0: -1}, cfg=cfg)
    adverse = run(bars, funding, {0: -1}, cfg=replace(cfg, intrabar_funding_policy="adverse"))
    assert default["summary"]["funding_btc"] == pytest.approx(.01)
    assert adverse["summary"]["funding_btc"] == 0
    assert adverse["audit"]["withheld_funding_credit_btc"] == pytest.approx(.01)
    assert adverse["audit"]["withheld_funding_events"] == 1
    assert default["events"][0] == adverse["events"][0]  # opening decision unaffected
    assert adverse["summary"]["final_btc"] == pytest.approx(default["summary"]["final_btc"] - .01)
    assert adverse["summary"]["ledger_error_btc"] == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize("rate,high,expected", [(-.01, 56_000, -.01), (.01, 51_000, .01)])
def test_adverse_policy_retains_debits_and_unambiguous_credits(rate, high, expected):
    result = run([bar(high=high)], [Funding(1800, rate, 50_000)], {0: -1},
                 cfg=replace(ZERO_COST, stop_pct=.1, intrabar_funding_policy="adverse"))
    assert result["summary"]["funding_btc"] == pytest.approx(expected)
    assert result["audit"]["withheld_funding_events"] == 0


def test_adverse_policy_does_not_remove_exact_open_credit_before_stop():
    result = run([bar(), bar(3600, high=56_000)], [Funding(3600, .01, 50_000)], {0: -1},
                 cfg=replace(ZERO_COST, stop_pct=.1, intrabar_funding_policy="adverse"))
    assert result["summary"]["funding_btc"] == pytest.approx(.01)
    assert result["audit"]["stops"] == 1


def test_adverse_policy_withholds_credit_when_debits_enable_liquidation():
    cfg = replace(ZERO_COST, max_exposure=2, intrabar_funding_policy="adverse")
    result = run([bar(low=40_000)], [Funding(600, -.1, 50_000), Funding(1800, .3, 50_000)],
                 {0: 2}, cfg=cfg)
    assert result["audit"]["withheld_funding_credit_btc"] == pytest.approx(.2)
    assert result["summary"]["funding_btc"] == pytest.approx(-.6)
    assert result["summary"]["liquidations"] == 1


def test_harmonic_average_add_reduce_and_reversal_reconcile():
    bars = [bar(), bar(3600, 60_000), bar(7200, 55_000), bar(10800, 52_000)]
    result = run(bars, [], {0: .2, 3600: .6, 7200: .3, 10800: -.5}, cfg=ZERO_COST)
    wallet = 1.0
    signed_quantity = reciprocal_basis = 0.0
    for event in result["events"]:
        delta, px = event["qty"], event["price"]
        if not signed_quantity or signed_quantity * delta > 0:
            reciprocal_basis += abs(delta) / px
        else:
            average_reciprocal = reciprocal_basis / abs(signed_quantity)
            wallet += math.copysign(1, signed_quantity) * abs(delta) * 100 * (average_reciprocal - 1 / px)
            reciprocal_basis -= abs(delta) * average_reciprocal
        signed_quantity += delta
    assert signed_quantity == 0
    assert result["summary"]["final_btc"] == pytest.approx(wallet)
    assert result["summary"]["trade_count"] == 2


def test_round_trip_charges_both_sides_and_initial_drawdown():
    cfg = replace(ZERO_COST, fee=.0005)
    result = run([bar()], [], {0: 1}, cfg=cfg)
    # 500 contracts would violate the cap after entry fees; 499 can be held.
    expected_fee = 2 * 499 * 100 / 50_000 * cfg.fee
    assert result["summary"]["fee_btc"] == pytest.approx(expected_fee)
    assert result["summary"]["final_btc"] == pytest.approx(1 - expected_fee)
    assert result["summary"]["max_drawdown_pct"] == pytest.approx(expected_fee * 100)


def test_stop_in_entry_bar_is_counted_and_final_close_is_not_duplicated():
    cfg = replace(ZERO_COST, stop_pct=.1)
    result = run([bar(high=51_000, low=44_000)], [], {0: 1}, cfg=cfg)
    assert result["audit"]["stops"] == 1
    assert len(result["trades"]) == 1
    assert result["trades"][0]["reason"] == "stop"
    assert result["events"][-1]["price"] == 45_000


def test_mark_triggers_stop_with_contract_basis_fill():
    first = bar()
    second = Bar(3600, 51_000, 51_000, 45_000, 48_000, 50_000, 50_000, 44_000, 47_000)
    result = run([first, second], [], {0: 1}, cfg=replace(ZERO_COST, stop_pct=.1))
    assert result["events"][-1]["price"] == 46_000


def test_gap_stop_uses_open_and_blocks_immediate_reentry():
    result = run([bar(), bar(3600, 40_000)], [], {0: 1, 3600: 1},
                 cfg=replace(ZERO_COST, stop_pct=.1))
    assert result["events"][-1]["price"] == 40_000
    assert result["audit"]["gap_exits"] == 1
    assert result["audit"]["targets_blocked_after_gap_exit"] == 1
    assert result["summary"]["fill_count"] == 2


def test_liquidation_gap_has_insurance_cost_and_preserves_insolvency():
    cfg = replace(ZERO_COST, liquidation_fee=.005)
    result = run([bar(), bar(3600, 20_000)], [], {0: 1}, cfg=cfg)
    assert result["summary"]["liquidations"] == 1
    assert result["summary"]["liquidation_fee_btc"] == pytest.approx(.0125)
    assert result["summary"]["final_btc"] < 0
    assert result["summary"]["bankrupt"]
    assert result["summary"]["max_drawdown_pct"] > 100


def test_nearer_stop_executes_before_intrabar_liquidation():
    result = run([bar(low=10_000)], [], {0: 1}, cfg=replace(ZERO_COST, stop_pct=.1))
    assert result["summary"]["liquidations"] == 0
    assert result["events"][-1]["reason"] == "stop"


def test_addition_never_loosens_long_stop():
    cfg = replace(ZERO_COST, stop_pct=.1)
    bars = [bar(), bar(3600, 48_000), bar(7200, 46_000, low=44_500)]
    result = run(bars, [], {0: .2, 3600: .8}, cfg=cfg)
    assert result["events"][-1]["reason"] == "stop"
    assert result["events"][-1]["price"] == 45_000


def test_addition_never_loosens_short_stop():
    cfg = replace(ZERO_COST, stop_pct=.1)
    bars = [bar(), bar(3600, 52_000), bar(7200, 54_000, high=55_500)]
    result = run(bars, [], {0: -.2, 3600: -.8}, cfg=cfg)
    assert result["events"][-1]["reason"] == "stop"
    assert result["events"][-1]["price"] == 55_000


def test_exposure_125_and_requested_clamp_with_integer_contracts():
    cfg = replace(ZERO_COST, max_exposure=1.25)
    result = run([bar()], [], {0: 9}, cfg=cfg)
    assert result["events"][0]["qty"] == 625
    assert result["audit"]["target_clamps"] == 1


def test_leverage_margin_caps_adding_even_when_exposure_allows_more():
    cfg = replace(ZERO_COST, max_exposure=4, leverage=2)
    result = run([bar()], [], {0: 4}, cfg=cfg)
    assert result["events"][0]["qty"] == 1_000
    assert result["audit"]["margin_caps"] == 1


def test_insufficient_btc_cannot_open_fractional_contract():
    result = run([bar()], [], {0: 1}, cfg=replace(ZERO_COST, initial_btc=.0001))
    assert result["summary"]["fill_count"] == 0
    assert result["audit"]["below_minimum"] == 1


def test_adverse_slippage_and_tick_rounding_reduce_roundtrip_wealth():
    result = run([bar()], [], {0: 1}, cfg=replace(ZERO_COST, slip_bps=3))
    assert result["events"][0]["price"] >= 50_015
    assert result["events"][-1]["price"] <= 49_985
    assert result["summary"]["final_btc"] < 1


@pytest.mark.parametrize("direction", [1, -1])
def test_entry_cap_includes_fees_and_marked_slippage(direction):
    cfg = replace(ZERO_COST, initial_btc=100, max_exposure=1.25,
                  fee=.001, slip_bps=100)
    result = run([bar()], [], {0: direction * 1.25}, cfg=cfg)
    assert result["audit"]["max_added_exposure"] <= 1.25


def test_mixed_positions_funding_and_costs_conserve_btc_wallet():
    rng = random.Random(9217)
    price, bars, targets, funding = 50_000.0, [], {}, []
    for i in range(256):
        close = price * rng.uniform(.95, 1.05)
        bars.append(bar(i * 3600, price, close=close,
                        high=max(price, close) * 1.02, low=min(price, close) * .98))
        if i % 4 == 0:
            targets[i * 3600] = rng.uniform(-1.5, 1.5)
        if i % 8 == 0:
            funding.append(Funding(i * 3600, rng.uniform(-.001, .001), price))
        price = close
    result = run(bars, funding, targets, cfg=Config(max_exposure=1.25))
    assert result["summary"]["net_btc"] == pytest.approx(sum(t["net_btc"] for t in result["trades"]))
    assert result["summary"]["ledger_error_btc"] == pytest.approx(0, abs=1e-12)
    assert result["equity_curve"][-1]["position"] == 0
    assert result["audit"]["max_added_exposure"] <= 1.25 + 1e-12


@pytest.mark.parametrize("bars,funding,targets,cfg", [
    ([], [], {}, ZERO_COST),
    ([bar(), bar(7200)], [], {}, ZERO_COST),
    ([bar()], [], {1: 1}, ZERO_COST),
    ([bar()], [], {0: math.nan}, ZERO_COST),
    ([bar()], [Funding(0, .01), Funding(0, .01)], {}, ZERO_COST),
    ([bar()], [], {}, replace(ZERO_COST, stop_pct=0)),
    ([bar()], [], {}, replace(ZERO_COST, intrabar_funding_policy="unknown")),
    ([replace(bar(), h=1)], [], {}, ZERO_COST),
])
def test_invalid_inputs_fail_closed(bars, funding, targets, cfg):
    with pytest.raises(ValueError):
        run(bars, funding, targets, cfg=cfg)
