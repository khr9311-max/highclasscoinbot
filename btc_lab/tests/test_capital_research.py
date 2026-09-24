from dataclasses import replace

import pytest

from btc_lab.capital_research import (CASES, INITIAL_BTC, PAPER_CANDIDATE,
                                      contract_diagnostics, simulation_config)
from btc_lab.engine import Bar, Config, Spec, run


def flat_bar(price=50_000):
    return Bar(0, price, price, price, price, price, price, price, price)


def test_actual_capital_cost_stress_preserves_strategy_and_frozen_choice():
    for name in CASES:
        base, stress = simulation_config(name, 1), simulation_config(name, 2)
        assert base.initial_btc == stress.initial_btc == .003
        assert base.fee == .0005 and stress.fee == .001
        assert stress.slip_bps == base.slip_bps * 2
        assert stress.stop_slip_bps == base.stop_slip_bps * 2
        assert stress.stop_pct == base.stop_pct == .20
        assert stress.max_exposure == base.max_exposure
        assert stress.intrabar_funding_policy == "adverse"
    assert PAPER_CANDIDATE == "momentum60_stop20"
    with pytest.raises(ValueError):
        simulation_config("new_strategy", 1)


def test_max_contracts_include_entry_and_stop_inside_same_hour():
    bar = replace(flat_bar(), l=40_000, mark_l=40_000)
    cfg = Config(initial_btc=.003, fee=0, slip_bps=0, stop_slip_bps=0)
    simulated = run([bar], [], {0: 1}, Spec(), cfg)
    assert simulated["equity_curve"][-1]["position"] == 0
    stats = contract_diagnostics(simulated, [bar], {0: 1}, Spec())
    assert stats["maximum_absolute_contracts"] == 1
    assert stats["one_step_exposure_pct_initial"] == pytest.approx(100 / (50_000 * INITIAL_BTC) * 100)


def test_integer_minimum_blocks_subcontract_target_at_actual_capital():
    bar = flat_bar(10_000)
    simulated = run([bar], [], {0: 1}, Spec(), simulation_config(PAPER_CANDIDATE, 1))
    stats = contract_diagnostics(simulated, [bar], {0: 1}, Spec())
    assert stats["maximum_absolute_contracts"] == 0
    assert stats["engine_below_minimum_count"] == 1
    assert stats["daily_targets_below_minimum_using_prior_close"] == 1
    assert stats["daily_target_count"] == 1
    assert stats["one_step_exposure_pct_daily_median"] == pytest.approx(100 / 30 * 100)
    assert not stats["nonpositive_equity_observed"]
