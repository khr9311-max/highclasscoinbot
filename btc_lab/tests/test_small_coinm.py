from dataclasses import asdict, replace
import random

import pytest

from btc_lab import engine
from btc_lab.small_coinm import Bar, Config, Funding, Spec, decision_diagnostics, simulate_policy


def bar(t=0, price=100, *, low=None, high=None):
    return Bar(t, price, high or price, low or price, price,
               price, high or price, low or price, price)


def config(policy, **kw):
    return replace(Config(initial_btc=1, fee=0, slip_bps=0, stop_slip_bps=0,
                          max_exposure=2, stop_pct=.5, sizing_policy=policy), **kw)


def test_original_control_is_exactly_equal_to_frozen_engine():
    bars = [bar(), bar(3600, 110), bar(7200, 105), bar(10800, 90, low=70)]
    funding = [Funding(3600, .001, 110)]
    targets = {0: .6, 3600: 1.4, 7200: -.7}
    cfg = config("original_floor", fee=.001, slip_bps=3, stop_slip_bps=10)
    original_cfg = engine.Config(**{k: v for k, v in asdict(cfg).items() if k != "sizing_policy"})
    assert simulate_policy(bars, funding, targets, Spec(), cfg) == engine.run(bars, funding, targets, Spec(), original_cfg)


def test_nearest_can_exceed_target_but_never_forces_subminimum_order():
    result = simulate_policy([bar()], [], {0: .6}, Spec(), config("nearest"))
    assert result["events"][0]["qty"] == 1
    diagnostics = decision_diagnostics(result, [bar()], {0: .6}, Spec())
    assert diagnostics["mean_absolute_tracking_error_pct_points"] == pytest.approx(40)
    below = simulate_policy([bar()], [], {0: .49}, Spec(), config("nearest"))
    assert below["events"] == []
    larger_minimum = simulate_policy([bar()], [], {0: .6}, Spec(min_qty=2), config("nearest"))
    assert larger_minimum["events"] == []


def test_hysteresis_retains_only_within_fixed_half_contract_band():
    bars = [bar(), bar(3600), bar(7200)]
    result = simulate_policy(bars, [], {0: 1.1, 3600: .8, 7200: .49}, Spec(), config("floor_hysteresis"))
    fills = [x for x in result["events"] if x["type"] == "fill"]
    assert [x["t"] for x in fills] == [0, 7200]
    assert result["audit"]["hysteresis_kept"] == 1


@pytest.mark.parametrize("policy", ["nearest", "floor_hysteresis"])
def test_overbudget_retention_is_forced_down_after_price_loss(policy):
    bars = [bar(), bar(3600, 90)]
    result = simulate_policy(bars, [], {0: 1, 3600: 1}, Spec(), config(policy, max_exposure=1))
    assert result["events"][1]["reason"] == "rebalance"
    assert result["events"][1]["position_after"] == 0
    assert result["audit"]["maximum_decision_exposure"] <= 1
    assert result["audit"]["budget_forced_reductions"] == 1


@pytest.mark.parametrize("policy", ["nearest", "floor_hysteresis"])
def test_after_cost_margin_and_maintenance_budgets_override_rounding(policy):
    cfg = config(policy, initial_btc=1.5, max_exposure=4, leverage=1, fee=.001, slip_bps=100)
    result = simulate_policy([bar()], [], {0: 4}, Spec(), cfg)
    assert result["events"][0]["qty"] == 1
    assert result["audit"]["maximum_decision_exposure"] <= 1
    # Even an overlarge configured leverage cannot override maintenance.
    high_margin = simulate_policy([bar()], [], {0: 4}, Spec(maint_margin_rate=.6),
                                  config(policy, max_exposure=4, leverage=5))
    assert high_margin["audit"]["maximum_decision_exposure"] < 1 / .6


@pytest.mark.parametrize("policy", ["nearest", "floor_hysteresis"])
def test_zero_target_and_reversal_bypass_hysteresis(policy):
    bars = [bar(), bar(3600), bar(7200)]
    result = simulate_policy(bars, [], {0: 1, 3600: -1, 7200: 0}, Spec(), config(policy))
    fills = [x for x in result["events"] if x["type"] == "fill"]
    assert [x["position_after"] for x in fills] == [1, 0, -1, 0]
    assert fills[1]["reason"] == "reverse"
    assert fills[-1]["reason"] == "flat"


@pytest.mark.parametrize("policy", ["nearest", "floor_hysteresis"])
def test_gap_protection_and_full_btc_ledger_remain_intact(policy):
    bars = [bar(), bar(3600, 70)]
    result = simulate_policy(bars, [Funding(3600, .001, 70)], {0: 1, 3600: 1},
                             Spec(), config(policy, stop_pct=.2, fee=.001, slip_bps=3))
    assert result["audit"]["gap_exits"] == 1
    assert result["audit"]["targets_blocked_after_gap_exit"] == 1
    assert result["events"][-1]["reason"] == "stop"
    assert result["summary"]["net_btc"] == pytest.approx(sum(t["net_btc"] for t in result["trades"]))
    assert result["summary"]["ledger_error_btc"] == pytest.approx(0, abs=1e-12)


def test_both_new_policies_respect_budgets_and_accounting_across_mixed_paths():
    rng = random.Random(32602)
    bars, targets, funding = [], {}, []
    price = 100.0
    for i in range(200):
        price *= rng.uniform(.95, 1.05)
        bars.append(bar(i*3600, price, low=price*.97, high=price*1.03))
        if i % 4 == 0:
            targets[i*3600] = rng.uniform(-2, 2)
        if i % 8 == 0:
            funding.append(Funding(i*3600, rng.uniform(-.002, .002), price))
    for policy in ("nearest", "floor_hysteresis"):
        result = simulate_policy(bars, funding, targets, Spec(), config(policy, fee=.001, slip_bps=10))
        assert result["audit"]["maximum_decision_exposure"] <= 2 + 1e-9
        assert result["summary"]["net_btc"] == pytest.approx(sum(t["net_btc"] for t in result["trades"]))
