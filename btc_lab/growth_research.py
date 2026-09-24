"""Long-horizon BTC research that permits losing subperiods.

Run: python -m btc_lab.growth_research
This is an exploratory comparison and paper-candidate manifest, not live trading.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from .engine import Config, run as simulate
from .growth_metrics import growth_metrics
from .research import (ROOT, FAMILIES, clean, completed_days, enrich, period_input,
                       read_market, save, target_schedule, ts)

# Fixed before evaluating this experiment. Two existing controls and four new cases.
# name, signal, annual volatility target, entry exposure cap, price stop fraction
CASES = (
    ('momentum25_stop10', 'momentum_20_60_120', .25, .75, .10),
    ('momentum40_stop10', 'momentum_20_60_120', .40, 1.25, .10),
    ('momentum40_stop20', 'momentum_20_60_120', .40, 1.25, .20),
    ('momentum60_stop20', 'momentum_20_60_120', .60, 2.00, .20),
    ('ensemble40_stop20', 'ensemble', .40, 1.25, .20),
    ('ensemble60_stop20', 'ensemble', .60, 2.00, .20),
)
PERIODS = {'training_through_2024': ('2021-04-01', '2025-01-01'),
           'recent': ('2025-01-01', None), 'continuous_full': ('2021-04-01', None)}


def targets_for_case(days, family, volatility, cap):
    if family != 'ensemble':
        return target_schedule(days, family, volatility, cap)
    schedules = [target_schedule(days, f, volatility, cap) for f in FAMILIES]
    common = set(schedules[0]).intersection(*(set(s) for s in schedules[1:]))
    # One net position. We do not sum separately funded hypothetical accounts.
    return {t: sum(s[t] for s in schedules)/len(schedules) for t in sorted(common)}


def choose_for_paper(rows, max_drawdown_pct=50):
    """Select on continuous training BTC growth; report later outcomes separately.

    Negative years/quarters and a negative cost-stress return do not veto a
    research candidate. Stress sensitivity is displayed. Bankruptcy/liquidation
    in either cost model is a different failure mode and blocks selection.
    """
    if not math.isfinite(max_drawdown_pct) or not 0 < max_drawdown_pct < 100:
        raise ValueError('Drawdown research band must be between 0 and 100')
    eligible = []
    for name, data in rows.items():
        train = data['training_through_2024']
        base, stress = train['base'], train['stress_2x']
        values = [base[k] for k in ('return_pct', 'cagr_pct', 'max_drawdown_pct')]
        if (all(math.isfinite(v) for v in values) and base['return_pct'] > 0
                and base['max_drawdown_pct'] <= max_drawdown_pct
                and all(not train[c]['bankrupt'] and train[c]['liquidations'] == 0
                        for c in ('base', 'stress_2x'))):
            eligible.append((base['cagr_pct'], name))
    winner = max(eligible)[1] if eligible else None
    flags = []
    if winner:
        stress = rows[winner]['training_through_2024']['stress_2x']
        if stress['return_pct'] <= 0:
            flags.append('training_profit_disappears_under_double_costs')
        if stress['max_drawdown_pct'] > max_drawdown_pct:
            flags.append('training_stress_exceeds_drawdown_band')
    return {'paper_candidate': winner, 'eligible_names': sorted(n for _, n in eligible),
            'selection_period': 'training_through_2024', 'drawdown_research_band_pct': max_drawdown_pct,
            'requires_every_subperiod_positive': False, 'stress_profit_is_required': False,
            'recent_used_for_selection': False, 'risk_flags': flags,
            'live_eligible': False, 'confidence': 'exploratory_reused_history'}


def risk_bands(rows):
    """Descriptive full-history comparison; never used to replace the training winner."""
    return {str(band): [name for name, data in rows.items()
                       if data['continuous_full']['base']['return_pct'] > 0
                       and data['continuous_full']['base']['max_drawdown_pct'] <= band
                       and not data['continuous_full']['base']['bankrupt']
                       and data['continuous_full']['base']['liquidations'] == 0]
            for band in (20, 35, 50)}


def frozen_protocol(output, cache, spec, quality):
    sources = [Path(__file__), Path(__file__).with_name('engine.py'),
               Path(__file__).with_name('research.py'), Path(__file__).with_name('growth_metrics.py')]
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(),
        'objective': 'Long-run net BTC growth while allowing temporary drawdowns and losing subperiods',
        'user_research_drawdown_preference_pct': 50, 'user_authorized_live_loss_limit': False,
        'initial_btc': .007, 'cases': CASES, 'periods': PERIODS,
        'new_cases': 4, 'previous_controls': 2, 'history_previously_viewed': True,
        'selection': 'Highest continuous training BTC CAGR with positive base total return, '
                     'base BTC DD<=50%, and no bankruptcy/liquidation in either cost model. '
                     'Negative years and stress losses are allowed and disclosed. No replacement using recent results.',
        'risk_bands_pct': [20, 35, 50], 'spec': asdict(spec), 'data_quality': quality,
        'costs': {'taker_fee': .0005, 'slippage_bps': 3, 'stop_slippage_bps': 10, 'stress_factor': 2},
        'ensemble': 'Equal average of causal momentum20/60/120, Donchian20/10, EMA20/100 target exposures',
        'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        'data_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(cache.iterdir())
                       if p.name.startswith(('BTCUSD_PERP', 'exchange_info_BTCUSD_PERP'))},
        'limitations': [
            'All history was already inspected; chronological splits are not a new untouched experiment.',
            'A 50% historical drawdown filter cannot guarantee a 50% future loss ceiling.',
            'Drawdown recovery is observed, not assumed. An unrecovered final episode is included in duration.',
            'Rolling 12-month windows overlap and are not independent win-probability samples.',
            'An ensemble combines correlated signals on one underlying; it is not asset diversification.',
            'Fixed stops can be crossed by gaps; shared wallet, maintenance and liquidation costs are approximations.',
            'Adverse intrabar funding policy; hourly close DD may understate intrahour DD.',
            'No real orders, account access, leverage configuration changes or validation-gate override.',
        ],
        'method_reference': 'https://www.nber.org/papers/w22208',
        'reference_scope': 'Volatility allocation research in other assets; not evidence of profitability for these BTC rules.'}
    path = output/'protocol.json'
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        strip = lambda x: {k:v for k,v in clean(x).items() if k!='created_utc'}
        if strip(old) != strip(protocol):
            raise ValueError('Protocol/source/data changed; use a new output folder')
        return old
    save(path, protocol)
    return protocol


def run(cache, output):
    output.mkdir(parents=True, exist_ok=True)
    bars, funding, spec, quality = read_market(cache)
    protocol = frozen_protocol(output, cache, spec, quality)
    end = bars[-1].t+3600
    days = completed_days(bars)
    rows = {}
    for name, family, volatility, cap, stop_fraction in CASES:
        print(name, flush=True)
        targets = targets_for_case(days, family, volatility, cap)
        rows[name] = {}
        for period, (start, finish) in PERIODS.items():
            b, f, t = period_input(bars, funding, targets, ts(start), ts(finish) if finish else end)
            rows[name][period] = {}
            for cost, factor in (('base', 1), ('stress_2x', 2)):
                cfg = Config(initial_btc=.007, fee=.0005*factor, slip_bps=3*factor,
                             stop_slip_bps=10*factor, max_exposure=cap, leverage=3,
                             stop_pct=stop_fraction, intrabar_funding_policy='adverse')
                result = simulate(b, f, t, spec, cfg)
                stats = enrich(result)
                stats['growth'] = growth_metrics(result)
                rows[name][period][cost] = stats
                save(output/f'{name}_{period}_{cost}.json', result)
        s = rows[name]['continuous_full']['base']
        print(f"  continuous BTC {s['return_pct']:+.2f}%, DD {s['max_drawdown_pct']:.2f}%", flush=True)
        save(output/'partial_results.json', rows)
    selection = choose_for_paper(rows)
    report = {'protocol': protocol, 'results': rows, 'selection': selection,
              'descriptive_full_history_risk_bands': risk_bands(rows)}
    save(output/'report.json', report)
    save(output/'paper_candidate.json', {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'research_manifest_not_a_running_paper_account', **selection,
        'parameters': next((case for case in CASES if case[0] == selection['paper_candidate']), None),
        'initial_btc_model': .007, 'orders_enabled': False,
        'prospective_evidence': 'None yet; these results precede any new forward observations.'})
    lines = ['# 손실 구간을 허용한 장기 BTC 성장 연구', '',
             '시작 0.007 BTC. 비용 포함 누적 수익. 최대낙폭은 시간봉 종가 BTC 평가 기준.', '']
    for period in PERIODS:
        lines += [f'## {period}', '',
                  '| 후보 | BTC 수익 | 비용2배 | 최대낙폭 | 최장 고점미회복(일) | 최악12개월 |',
                  '|---|---:|---:|---:|---:|---:|']
        for name, data in rows.items():
            s = data[period]['base']; stress = data[period]['stress_2x']; g=s['growth']
            worst = g['worst_12m_return_pct']
            w = f'{worst:+.2f}%' if worst is not None else '표본 부족'
            lines.append(f"| {name} | {s['return_pct']:+.2f}% | {stress['return_pct']:+.2f}% | "
                         f"{s['max_drawdown_pct']:.2f}% | {g['max_underwater_days']:.1f} | {w} |")
        lines.append('')
    lines += ['## 시간순 연구 후보', '', json.dumps(selection, ensure_ascii=False), '',
              '후보는 2024년까지의 연속 계좌로 선택한다. 최근/전체 결과를 보고 교체하지 않는다.',
              '특정 연도 손실과 비용 스트레스 손실은 연구 탈락 조건이 아니다.',
              '개별 기간은 독립 재시작이고 continuous_full은 처음부터 끝까지 하나의 계좌다.',
              '최장 미회복 기간에는 끝까지 미회복인 기간이 포함된다. 새 미래 성과의 보장은 없다.']
    (output/'comparison.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=ROOT/'binance_coinm_v1/state/cache')
    parser.add_argument('--output', type=Path, default=ROOT/'btc_lab/state/growth_20260924')
    args = parser.parse_args()
    run(args.cache, args.output)
