"""Post-hoc +/-10% capital robustness diagnostic, never allocation optimization."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from . import small_coinm
from .research import clean, completed_days, enrich, period_input, read_market, save, target_schedule, ts
from .small_capital_report import TOTAL_BTC, account_metrics

ROOT = Path(__file__).resolve().parents[1]
CAPITALS = (.0027, .0033)
PERIODS = {'continuous_from_20210401':'2021-04-01', 'recent_reset_20250101':'2025-01-01'}


def run(cache, output):
    output.mkdir(parents=True, exist_ok=True)
    if (output/'protocol.json').exists():
        raise FileExistsError('Use a new directory for another diagnostic; prior records are immutable')
    bars,funding,original_spec,quality = read_market(cache)
    spec = replace(original_spec, maint_margin_rate=.004)
    sources = (Path(__file__), Path(small_coinm.__file__), Path(__file__).with_name('engine.py'),
               Path(__file__).with_name('research.py'), Path(__file__).with_name('small_capital_report.py'))
    protocol = {'created_utc':datetime.now(timezone.utc).isoformat(),
                'diagnostic':'Post-hoc capital robustness after observing .003 BTC hysteresis results',
                'initial_capitals':CAPITALS, 'periods':PERIODS, 'runs':8,
                'policy':'floor_hysteresis', 'band_contracts':.5, 'signal':'momentum20/60/120',
                'annual_vol_target':.6, 'max_exposure':2, 'leverage':3, 'stop_pct':.2,
                'base_fee':.0005, 'slip_bps':3, 'stop_slip_bps':10,
                'stress_factor':2, 'spec':asdict(spec), 'data_quality':quality,
                'orders_enabled':False, 'allocation_changed':False, 'selection_performed':False,
                'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                'data_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(cache.iterdir())
                    if p.name.startswith(('BTCUSD_PERP','exchange_info_BTCUSD_PERP'))},
                'limitations':['Historical data and central outcome were already viewed; diagnostic is not blind evidence.',
                               'No search for a profitable account balance or recommendation to deposit additional capital.',
                               'Reserve equals current account BTC minus the modeled budget; no transfers are performed.']}
    save(output/'protocol.json', protocol)
    targets = target_schedule(completed_days(bars), 'momentum_20_60_120', .6, 2)
    rows = []
    for capital in CAPITALS:
        for period,begin in PERIODS.items():
            b,f,t = period_input(bars,funding,targets,ts(begin),bars[-1].t+3600)
            for cost,factor in (('base',1),('stress_2x',2)):
                cfg = small_coinm.Config(initial_btc=capital, fee=.0005*factor, slip_bps=3*factor,
                    stop_slip_bps=10*factor, max_exposure=2, leverage=3, stop_pct=.2,
                    intrabar_funding_policy='adverse', sizing_policy='floor_hysteresis')
                result = small_coinm.simulate_policy(b,f,t,spec,cfg)
                name = f'capital_{capital:.4f}_{period}_{cost}.json'
                save(output/name,result)
                row = {'capital_btc':capital, 'period':period, 'cost':cost,
                       'allocation_summary':enrich(result),
                       'whole_wallet':account_metrics(result,TOTAL_BTC-capital), 'artifact':name}
                rows.append(row)
                print(json.dumps({'capital':capital,'period':period,'cost':cost,
                    'return_pct':row['allocation_summary']['return_pct'],
                    'dd_pct':row['allocation_summary']['max_drawdown_pct']}),flush=True)
    report = {'protocol':protocol,'results':rows}
    save(output/'report.json',report)
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,default=ROOT/'binance_coinm_v1/state/cache')
    parser.add_argument('--output',type=Path,default=ROOT/'btc_lab/state/small_sensitivity_20260924')
    args=parser.parse_args()
    run(args.cache,args.output)
