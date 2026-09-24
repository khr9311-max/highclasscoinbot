"""Read-only account inventory and a reviewable capital plan. Never sends orders.

Explicit command: python -m btc_lab.preflight --private
Only this command loads the Binance-specific credentials. Research/forward do not.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import json
import math
from pathlib import Path
import sys

SYMBOL = 'BTCUSD_PERP'
BASES = {'spot': 'https://api.binance.com', 'coinm': 'https://dapi.binance.com',
         'usdm': 'https://fapi.binance.com'}
PRIVATE_GETS = {
    'spot': {'/api/v3/account', '/api/v3/openOrders', '/sapi/v1/asset/wallet/balance',
             '/sapi/v1/account/apiRestrictions'},
    'coinm': {'/dapi/v1/account', '/dapi/v1/positionRisk', '/dapi/v1/positionSide/dual',
              '/dapi/v1/openOrders', '/dapi/v1/openAlgoOrders', '/dapi/v1/commissionRate',
              '/dapi/v2/leverageBracket'},
    'usdm': {'/fapi/v3/account'},
}
PUBLIC_GETS = {'/dapi/v1/time', '/dapi/v1/premiumIndex', '/dapi/v1/exchangeInfo'}
REQUIRED_SECTIONS = {'spot', 'spot_orders', 'wallets', 'permissions', 'coinm', 'positions',
                     'position_mode', 'coinm_orders', 'coinm_algo_orders', 'fees', 'brackets', 'usdm'}


def deny_writes(*args, **kwargs):
    raise PermissionError('Account preparation is read-only')


def numeric(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Nonfinite account data')
    return result


def fields(row, names):
    return {name: row.get(name) for name in names}


def premium_for_symbol(payload):
    rows = payload if isinstance(payload, list) else [payload]
    matches = [row for row in rows if isinstance(row, dict) and row.get('symbol') == SYMBOL]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous premium index symbol')
    if numeric(matches[0]['markPrice']) <= 0 or numeric(matches[0]['indexPrice']) <= 0:
        raise ValueError('Invalid premium index prices')
    return matches[0]


def order_counts(data):
    if isinstance(data, dict):
        data = data.get('orders', data.get('data'))
    if not isinstance(data, list):
        raise ValueError('Missing order list')
    by_symbol = {}
    for row in data:
        name = row.get('symbol', 'UNKNOWN')
        by_symbol[name] = by_symbol.get(name, 0)+1
    return {'count': len(data), 'by_symbol': by_symbol}


def sanitize(label, data):
    """Persist only inventory fields; no raw account response, IDs or credentials."""
    if label == 'spot':
        return {'can_trade': data.get('canTrade'), 'balances': [
            fields(a, ('asset', 'free', 'locked')) for a in data['balances']
            if numeric(a['free']) or numeric(a['locked'])]}
    if label == 'wallets':
        return [fields(a, ('walletName', 'balance', 'activate')) for a in data]
    if label == 'permissions':
        return fields(data, ('enableReading', 'enableFutures', 'enableWithdrawals',
                             'enableSpotAndMarginTrading', 'ipRestrict'))
    if label in ('coinm', 'usdm'):
        return {'can_trade': data.get('canTrade'), 'assets': [
            fields(a, ('asset', 'walletBalance', 'marginBalance', 'availableBalance',
                       'initialMargin', 'maintMargin')) for a in data['assets']
            if a['asset'] == 'BTC' or numeric(a['walletBalance']) or numeric(a['marginBalance'])],
            'active_positions': [fields(p, ('symbol', 'positionAmt', 'positionSide',
                                             'initialMargin', 'maintMargin'))
                                 for p in data.get('positions', [])
                                 if numeric(p.get('positionAmt', 0)) or numeric(p.get('initialMargin', 0))]}
    if label == 'positions':
        return [fields(p, ('symbol', 'positionAmt', 'positionSide', 'entryPrice', 'markPrice',
                           'liquidationPrice', 'leverage', 'marginType', 'isolatedMargin'))
                for p in data if numeric(p.get('positionAmt', 0)) or p.get('symbol') == SYMBOL]
    if label == 'position_mode':
        if not isinstance(data.get('dualSidePosition'), bool):
            raise ValueError('Unknown position mode')
        return {'hedge_mode': data['dualSidePosition']}
    if label.endswith('_orders'):
        return order_counts(data)
    if label == 'fees':
        return {'maker': numeric(data['makerCommissionRate']), 'taker': numeric(data['takerCommissionRate'])}
    if label == 'brackets':
        rows = data if isinstance(data, list) else [data]
        row = next(r for r in rows if r.get('symbol') == SYMBOL)
        brackets = row['brackets']
        if not brackets:
            raise ValueError('No maintenance brackets')
        for bracket in brackets:
            if not 0 < numeric(bracket['maintMarginRatio']) < 1:
                raise ValueError('Invalid maintenance bracket')
        return {'symbol': SYMBOL, 'brackets': [fields(b, ('bracket', 'initialLeverage', 'qtyCap',
                        'qtyFloor', 'maintMarginRatio', 'cum')) for b in brackets]}
    raise ValueError('Unknown inventory section')


async def collect(settings):
    from binance_coinm_v1.exchange.rest_client import BinanceRestClient
    from binance_coinm_v1.storage.redact import GLOBAL_REDACTOR
    if settings.binance_env != 'live':
        raise ValueError('Real account preparation requires BINANCE_ENV=live')
    if not settings.has_api_keys:
        raise ValueError('Binance-specific API credentials are missing')
    GLOBAL_REDACTOR.add(*settings.secrets())
    clients = {name: BinanceRestClient(base, settings.api_key, settings.api_secret,
                   mutation_guard=deny_writes, max_get_retries=1) for name, base in BASES.items()}
    async def public(path, params=None):
        if path not in PUBLIC_GETS:
            raise PermissionError('Public endpoint not allowed')
        return await clients['coinm'].get_public(path, params)
    async def private(label, market, path, params=None):
        if path not in PRIVATE_GETS[market]:
            raise PermissionError('Private GET endpoint not allowed')
        try:
            data = await clients[market].signed('GET', path, params)
            return label, {'ok': True, 'data': sanitize(label, data)}
        except Exception as exc:
            # Provider text can contain signed URLs. Never serialize exception text.
            return label, {'ok': False, 'error_type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
    try:
        before = datetime.now(timezone.utc).timestamp()
        clock = await public('/dapi/v1/time')
        after = datetime.now(timezone.utc).timestamp()
        for client in clients.values():
            client.time_offset_ms = int(clock['serverTime']-(before+after)*500)
        premium, info = await asyncio.gather(public('/dapi/v1/premiumIndex', {'symbol': SYMBOL}),
                                             public('/dapi/v1/exchangeInfo'))
        premium = premium_for_symbol(premium)
        contract = next(s for s in info['symbols'] if s['symbol'] == SYMBOL)
        requests = (
            ('spot', 'spot', '/api/v3/account', None),
            ('spot_orders', 'spot', '/api/v3/openOrders', None),
            ('wallets', 'spot', '/sapi/v1/asset/wallet/balance', None),
            ('permissions', 'spot', '/sapi/v1/account/apiRestrictions', None),
            ('coinm', 'coinm', '/dapi/v1/account', None),
            ('positions', 'coinm', '/dapi/v1/positionRisk', None),
            ('position_mode', 'coinm', '/dapi/v1/positionSide/dual', None),
            ('coinm_orders', 'coinm', '/dapi/v1/openOrders', None),
            ('coinm_algo_orders', 'coinm', '/dapi/v1/openAlgoOrders', None),
            ('fees', 'coinm', '/dapi/v1/commissionRate', {'symbol': SYMBOL}),
            ('brackets', 'coinm', '/dapi/v2/leverageBracket', {'symbol': SYMBOL}),
            ('usdm', 'usdm', '/fapi/v3/account', None),
        )
        sections = dict(await asyncio.gather(*(private(*req) for req in requests)))
        return {'observed_utc': datetime.now(timezone.utc).isoformat(), 'read_only': True,
                'symbol': SYMBOL, 'market': {'mark': numeric(premium['markPrice']),
                    'index': numeric(premium['indexPrice']), 'contract_size': numeric(contract['contractSize']),
                    'filters': contract['filters']}, 'sections': sections,
                'local_execution_mode': settings.execution_mode,
                'snapshot_scope': 'Spot, COIN-M, USD-M and wallet inventory; not proof of order permission or execution readiness'}
    finally:
        await asyncio.gather(*(c.close() for c in clients.values()))


def capital_plan(snapshot):
    sections = snapshot['sections']
    def data(name, default):
        return sections[name].get('data', default) if sections.get(name, {}).get('ok') else default
    blockers = [f'account_section_unconfirmed:{k}' for k in sorted(REQUIRED_SECTIONS)
                if not sections.get(k, {}).get('ok')]
    spot = data('spot', {}).get('balances', [])
    coinm = data('coinm', {}).get('assets', [])
    spot_free = sum(numeric(a['free']) for a in spot if a['asset'] == 'BTC')
    cm_free = sum(numeric(a['availableBalance']) for a in coinm if a['asset'] == 'BTC')
    available = max(0, spot_free)+max(0, cm_free)
    # Initial deployment is capped at .003 BTC and 75% of the observed available BTC.
    budget = float(min(Decimal('.003'), Decimal(str(available))*Decimal('.75')).quantize(
                   Decimal('.00000001'), rounding=ROUND_DOWN))
    if not sections.get('spot', {}).get('ok') or not sections.get('coinm', {}).get('ok'):
        budget = None
    if data('coinm', {}).get('can_trade') is not True:
        blockers.append('coinm_can_trade_unconfirmed_or_false')
    if data('permissions', {}).get('enableFutures') is not True:
        blockers.append('futures_api_permission_unconfirmed_or_false')
    if data('position_mode', {}).get('hedge_mode') is not False:
        blockers.append('one_way_mode_unconfirmed_or_false')
    if any(numeric(p.get('positionAmt', 0)) for p in data('positions', [])):
        blockers.append('existing_coinm_positions_require_reconciliation')
    for section in ('coinm_orders', 'coinm_algo_orders', 'spot_orders'):
        if data(section, {}).get('count', 0):
            blockers.append(f'existing_{section}')
    if data('usdm', {}).get('active_positions'):
        blockers.append('other_futures_positions_require_review')
    if budget is None or budget <= 0:
        blockers.append('capital_not_available_or_unconfirmed')
    elif cm_free < budget:
        blockers.append('proposed_btc_capital_is_not_in_coinm_wallet')
    blockers += ['new_daily_target_live_executor_not_implemented',
                 'shared_collateral_execution_and_protection_not_validated',
                 'new_strategy_live_readiness_not_passed', 'pc_forward_observation_not_completed']
    fees = data('fees', {})
    mark = snapshot['market']['mark']
    cs = snapshot['market']['contract_size']
    return {'created_utc': snapshot['observed_utc'], 'observed_spot_free_btc': spot_free,
            'observed_coinm_available_btc': cm_free, 'proposed_strategy_capital_btc': budget,
            'reserve_btc': float(Decimal(str(available))-Decimal(str(budget))) if budget is not None else None,
            'proposed_transfer_btc': max(0, budget-cm_free) if budget is not None else None,
            'proposed_transfer_is_executed': False, 'strategy_candidate': 'momentum60_stop20',
            'comparison_candidate': 'momentum40_stop20', 'ai_role': 'information_only_no_trading_effect',
            'options_status': 'not_enabled_or_implemented_for_this_deployment',
            'actual_commission': fees,
            'paper_exposure_cap': 2, 'paper_price_stop_pct': 20,
            'future_initial_live_contract_cap_proposal': 1,
            'one_contract_notional_usd': cs,
            'one_contract_initial_margin_btc_at_3x': cs/mark/3,
            'one_contract_long_stop_loss_btc_at_minus20pct_excluding_costs': cs/mark*(1/.8-1),
            'one_contract_short_stop_loss_btc_at_plus20pct_excluding_costs': cs/mark*(1-1/1.2),
            'live_orders_enabled': False, 'live_ready': False, 'blockers': blockers,
            'note': 'Snapshot-derived proposal, not an instruction to transfer or trade. Whole-wallet margin model differs from V1 isolated runtime.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private', action='store_true', required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent/'state'/
                        ('preflight_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')))
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Use a new output folder to preserve previous account snapshots')
    from binance_coinm_v1.config import Settings
    snapshot = asyncio.run(collect(Settings.load()))
    plan = capital_plan(snapshot)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, obj in (('account_snapshot.json', snapshot), ('capital_plan.json', plan)):
        with (args.output/name).open('x', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'output': str(args.output), 'plan': plan}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'code': getattr(exc, 'code', None)}))
        sys.exit(2)
