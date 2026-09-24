import pytest

from btc_lab.small_capital_report import account_metrics


def path(values):
    return {'summary':{'initial_btc':values[0]},
            'equity_curve':[{'t':i*86400, 'equity_btc':v} for i,v in enumerate(values)]}


def test_reserve_is_never_spent_to_refill_the_strategy():
    result = account_metrics(path([.003, 0]), .00112273)
    assert result['final_total_btc'] == .00112273
    assert not result['reserve_available_for_margin']
    assert result['total_return_pct'] > -100


def test_whole_account_drawdown_is_recomputed_from_its_own_peak():
    result = account_metrics(path([1, 2, 1]), 1)
    assert result['total_max_drawdown_pct'] == pytest.approx(100/3)
    assert result['total_max_drawdown_pct'] != pytest.approx(50*.5)
    assert result['total_return_pct'] == 0


def test_missing_initial_equity_or_nonfinite_values_rejected():
    broken = path([1, 2])
    broken['equity_curve'][0]['equity_btc'] = 2
    with pytest.raises(ValueError):
        account_metrics(broken)
    with pytest.raises(ValueError):
        account_metrics(path([1, float('nan')]))
