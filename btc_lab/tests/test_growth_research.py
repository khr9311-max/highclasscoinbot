from copy import deepcopy

import pytest

from btc_lab.growth_research import choose_for_paper, targets_for_case
from btc_lab.research import FAMILIES, target_schedule
from btc_lab.tests.test_research import days


def score(gain, dd=40):
    return dict(return_pct=gain, cagr_pct=gain/4, max_drawdown_pct=dd,
                liquidations=0, bankrupt=False, quarter_returns_pct={'2022Q1': -20})


def row(gain):
    return {'training_through_2024': {'base': score(gain), 'stress_2x': score(-5)},
            'recent': {'base': score(-10)}, 'continuous_full': {'base': score(10)}}


def test_temporary_losses_and_stress_loss_do_not_veto_paper_candidate():
    selection = choose_for_paper({'candidate': row(30)})
    assert selection['paper_candidate'] == 'candidate'
    assert 'training_profit_disappears_under_double_costs' in selection['risk_flags']
    assert not selection['live_eligible']


def test_fifty_percent_band_rejects_excess_dd_but_accepts_equality():
    data = row(30)
    data['training_through_2024']['base']['max_drawdown_pct'] = 50
    assert choose_for_paper({'a': data})['paper_candidate'] == 'a'
    data['training_through_2024']['base']['max_drawdown_pct'] = 50.01
    assert choose_for_paper({'a': data})['paper_candidate'] is None


def test_recent_winner_cannot_replace_training_choice():
    rows = {'a': row(30), 'b': row(20)}
    before = choose_for_paper(rows)
    changed = deepcopy(rows)
    changed['b']['recent']['base'] = score(1000)
    changed['a']['recent']['base'] = score(-90)
    assert choose_for_paper(changed) == before
    assert before['paper_candidate'] == 'a'


def test_liquidation_and_negative_total_are_not_temporary_loss_evidence():
    assert choose_for_paper({'a': row(-2)})['paper_candidate'] is None
    data = row(20)
    data['training_through_2024']['stress_2x']['liquidations'] = 1
    assert choose_for_paper({'a': data})['paper_candidate'] is None


def test_ensemble_is_one_net_target_and_cannot_see_future_days():
    history = days()
    target = targets_for_case(history, 'ensemble', .6, 2)
    singles = [target_schedule(history, f, .6, 2) for f in FAMILIES]
    assert target == pytest.approx({t:sum(s[t] for s in singles)/3 for t in target})
    prefix = targets_for_case(history[:280], 'ensemble', .6, 2)
    assert prefix == {t:v for t,v in target.items() if t<=history[279].t+86400}
    assert max(map(abs,target.values())) <= 2
