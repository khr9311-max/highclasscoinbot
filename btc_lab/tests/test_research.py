"""Causal daily portfolio targets and a selection process excluding recent results."""
from copy import deepcopy

import numpy as np
import pytest

from btc_lab.engine import Bar
from btc_lab.research import Day, FAMILIES, completed_days, enrich, pick_development, target_schedule, ts


def days(n=360):
    p=100*np.exp(np.cumsum(np.random.default_rng(941).normal(.0006,.025,n)))
    return [Day(i*86400,float(v),float(v*1.02),float(v*.98),float(v)) for i,v in enumerate(p)]


@pytest.mark.parametrize('family',(*FAMILIES,'long_vol_benchmark'))
def test_new_future_days_cannot_change_past_targets(family):
    history=days()
    prefix=history[:280]
    all_targets=target_schedule(history,family,.4,1.25)
    earlier=target_schedule(prefix,family,.4,1.25)
    assert earlier
    assert earlier=={t:v for t,v in all_targets.items() if t<=prefix[-1].t+86400}
    assert min(earlier)>=201*86400
    assert max(map(abs,all_targets.values()))<=1.25


def test_complete_day_discards_leading_and_trailing_partial_days():
    bars=[Bar(t=i*3600,o=100,h=101,l=99,c=100,mark_o=100,mark_h=101,mark_l=99,mark_c=100)
          for i in range(3,53)]
    result=completed_days(bars)
    assert len(result)==1 and result[0].t==86400


def test_discontinuous_daily_history_does_not_create_recent_target():
    history=days()
    history.pop(260)
    targets=target_schedule(history,'momentum_20_60_120',.25,.75)
    assert 300*86400 not in targets


def score(ret):
    return dict(return_pct=ret,max_drawdown_pct=10,bankrupt=False,liquidations=0,cagr_pct=ret)


def record(train,validation,recent):
    return {p:{'base':score(v),'stress_2x':score(v/2)}
            for p,v in [('development',train),('validation_2024',validation),('recent',recent)]}


def test_selection_rejects_failed_validation_instead_of_switching_to_recent_winner():
    rows={'a':record(30,-1,-10),'b':record(20,10,100),'long_vol_benchmark':record(50,50,50)}
    result=pick_development(rows)
    assert result['development_choice']=='a'
    assert result['passed_validation_screen'] is False
    assert result['live_eligible'] is False
    changed=deepcopy(rows)
    changed['b']['recent']['base']=score(-100)
    assert pick_development(changed)==result


def test_development_cost_stress_and_drawdown_are_required():
    rows={'a':record(30,10,100)}
    rows['a']['development']['stress_2x']=score(-1)
    assert pick_development(rows)['development_choice'] is None
    rows['a']['development']['stress_2x']=score(10)
    rows['a']['development']['base']['max_drawdown_pct']=36
    assert pick_development(rows)['development_choice'] is None


def test_midnight_equity_belongs_to_day_and_quarter_just_ended():
    start=int(ts('2024-03-31'))
    curve=[dict(t=start+i*3600,equity_btc=1+i/240,position=0,exposure=0)
           for i in range(25)]
    report=enrich(dict(summary=dict(initial_btc=1,final_btc=1.1),equity_curve=curve))
    assert report['days']==1
    assert report['quarter_returns_pct']==pytest.approx({'2024Q1':10})
