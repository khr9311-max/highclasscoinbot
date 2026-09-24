from copy import deepcopy
import json

import pytest

from btc_lab.preflight import capital_plan, deny_writes, premium_for_symbol, sanitize


def snapshot():
    sections = {name: {'ok': True, 'data': value} for name,value in {
        'spot': {'balances': [{'asset':'BTC','free':'0.00412273','locked':'0'}]},
        'coinm': {'can_trade':True,'assets':[{'asset':'BTC','availableBalance':'0'}]},
        'permissions': {'enableFutures':True}, 'positions': [],
        'position_mode': {'hedge_mode':False}, 'usdm': {'active_positions':[]},
        'fees': {'maker':.0002,'taker':.0005}, 'coinm_orders': {'count':0},
        'coinm_algo_orders': {'count':0}, 'spot_orders': {'count':0},
    }.items()}
    return {'sections':sections,'observed_utc':'2026-09-24T12:00:00+00:00',
            'market':{'mark':80000,'contract_size':100}}


def test_capital_plan_preserves_reserve_and_never_transfers():
    plan=capital_plan(snapshot())
    assert plan['proposed_strategy_capital_btc']==.003
    assert plan['reserve_btc']==pytest.approx(.00112273)
    assert plan['proposed_transfer_btc']==.003
    assert not plan['live_ready'] and not plan['live_orders_enabled']
    assert not plan['proposed_transfer_is_executed']


def test_missing_account_is_unknown_not_zero_capital():
    data=snapshot()
    data['sections']['spot']={'ok':False,'error_type':'TimeoutError'}
    plan=capital_plan(data)
    assert plan['proposed_strategy_capital_btc'] is None
    assert 'account_section_unconfirmed:spot' in plan['blockers']


def test_existing_other_symbol_position_and_orders_are_blockers():
    data=snapshot()
    data['sections']['positions']['data']=[{'symbol':'BTCUSD_261225','positionAmt':'1'}]
    data['sections']['coinm_algo_orders']['data']={'count':1}
    blockers=capital_plan(data)['blockers']
    assert 'existing_coinm_positions_require_reconciliation' in blockers
    assert 'existing_coinm_algo_orders' in blockers


def test_account_permissions_and_margin_mode_are_not_inferred_from_balance():
    data=snapshot()
    data['sections']['permissions']['data']={}
    data['sections']['position_mode']['data']={'hedge_mode':True}
    plan=capital_plan(data)
    assert 'futures_api_permission_unconfirmed_or_false' in plan['blockers']
    assert 'one_way_mode_unconfirmed_or_false' in plan['blockers']


def test_output_is_allowlisted_and_order_identifiers_are_not_persisted():
    raw={'canTrade':True,'apiKey':'SECRET','uid':'private',
         'balances':[{'asset':'BTC','free':'1','locked':'0','secret':'SECRET'}]}
    assert 'SECRET' not in json.dumps(sanitize('spot',raw))
    assert sanitize('coinm_orders',[{'symbol':'BTCUSD_PERP','orderId':999}])=={
        'count':1,'by_symbol':{'BTCUSD_PERP':1}}


def test_private_writes_always_rejected():
    for method in ('POST','PUT','DELETE'):
        with pytest.raises(PermissionError):
            deny_writes(method,'/dapi/v1/order',{})


def test_nonfinite_account_value_rejected():
    with pytest.raises(ValueError):
        sanitize('spot',{'balances':[{'asset':'BTC','free':'NaN','locked':'0'}]})


def test_inverse_long_and_short_stop_losses_are_not_linear():
    plan=capital_plan(snapshot())
    assert plan['one_contract_long_stop_loss_btc_at_minus20pct_excluding_costs']==pytest.approx(.0003125)
    assert plan['one_contract_short_stop_loss_btc_at_plus20pct_excluding_costs']==pytest.approx(1/4800)


@pytest.mark.parametrize('wrap', [lambda x: x, lambda x: [x]])
def test_premium_index_accepts_both_documented_response_shapes(wrap):
    row = {'symbol': 'BTCUSD_PERP', 'markPrice': '80000', 'indexPrice': '80002'}
    assert premium_for_symbol(wrap(row)) == row


def test_ambiguous_or_wrong_premium_symbol_rejected():
    row = {'symbol': 'BTCUSD_PERP', 'markPrice': '80000', 'indexPrice': '80002'}
    for payload in ([], [row, row], {'symbol': 'ETHUSD_PERP'}):
        with pytest.raises(ValueError):
            premium_for_symbol(payload)


def test_omitted_inventory_section_does_not_silently_pass():
    data = snapshot()
    del data['sections']['coinm_orders']
    assert 'account_section_unconfirmed:coinm_orders' in capital_plan(data)['blockers']
