"""Combine fixed studies in total-account BTC units, including idle BTC reserve.

Pure local report. No candidate selection, account access, orders or re-sizing.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOTAL_BTC = .00412273
ALLOCATED_BTC = .003
RESERVE_BTC = .00112273


def account_metrics(result, reserve_btc=RESERVE_BTC):
    if not math.isfinite(reserve_btc) or reserve_btc < 0:
        raise ValueError('Reserve must be finite and nonnegative')
    curve = result['equity_curve']
    initial = result['summary']['initial_btc']
    if not curve or not math.isfinite(initial) or initial <= 0:
        raise ValueError('Initial account and observed equity are required')
    if not math.isclose(curve[0]['equity_btc'], initial, abs_tol=1e-12):
        raise ValueError('Curve must include the pre-trade initial capital')
    initial_total = initial + reserve_btc
    peak = initial_total
    drawdown = 0.0
    for point in curve:
        value = point['equity_btc'] + reserve_btc
        if not math.isfinite(value):
            raise ValueError('Nonfinite account equity')
        peak = max(peak, value)
        drawdown = max(drawdown, (peak-value)/peak)
    final_total = curve[-1]['equity_btc'] + reserve_btc
    years = (curve[-1]['t']-curve[0]['t'])/(365.25*86400)
    return {'initial_total_btc':initial_total, 'allocated_btc':initial,
            'untouched_reserve_btc':reserve_btc, 'final_total_btc':final_total,
            'total_btc_gain':final_total-initial_total,
            'total_return_pct':(final_total/initial_total-1)*100,
            'total_max_drawdown_pct':drawdown*100,
            'total_cagr_pct':((final_total/initial_total)**(1/years)-1)*100
                if years > 0 and final_total > 0 else None,
            'reserve_available_for_margin':False,
            'reserve_is_hypothetical_constant_btc':True}


def build(coinm, spot, output):
    output.mkdir(parents=True, exist_ok=True)
    if (output/'report.json').exists():
        raise FileExistsError('Preserve prior comparisons; use a new output directory')
    rows, evidence = [], {}
    for instrument, directory in (('coinm',coinm), ('spot',spot)):
        report_path = directory/'report.json'
        report = json.loads(report_path.read_text(encoding='utf-8'))
        evidence[str(report_path.relative_to(ROOT))] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        for case, periods in report['results'].items():
            for period, costs in periods.items():
                for cost in costs:
                    path = directory/f'{case}_{period}_{cost}.json'
                    result = json.loads(path.read_text(encoding='utf-8'))
                    if not math.isclose(result['summary']['initial_btc'], ALLOCATED_BTC, abs_tol=1e-12):
                        raise ValueError('Comparisons must use the same fixed allocation')
                    evidence[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
                    rows.append({'instrument':instrument, 'case':case,'period':period,'cost':cost,
                                 'allocation_summary':result['summary'],
                                 'whole_wallet':account_metrics(result), 'artifact':str(path.relative_to(ROOT))})
    combined = {'created_utc':datetime.now(timezone.utc).isoformat(), 'rows':rows,
                'initial_total_btc':TOTAL_BTC, 'planned_allocation_btc':ALLOCATED_BTC,
                'reserve_btc':RESERVE_BTC, 'orders_enabled':False, 'selection_performed':False,
                'evidence_sha256':evidence, 'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'limitations':['BTC reserve is constant and is not used as margin or to refill a losing strategy.',
                               'Account drawdown is recomputed from the combined curve, not multiplied by allocation weight.',
                               'Spot and COIN-M have different collateral, fee, signal and execution assumptions.',
                               'BTC-valued quote dust is not actual held BTC; inspect the spot allocation summary.',
                               'All studies reuse historical data; this report does not choose or authorize live trading.']}
    (output/'report.json').write_text(json.dumps(combined,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    lines = ['# 소액 거래 방식의 계좌 전체 BTC 평가', '',
             '운용0.003 BTC + 사용하지 않는 예비분0.00112273 BTC. 총0.00412273 BTC 기준.',
             '전체계좌 낙폭은 합산 평가곡선으로 다시 계산했다. 미래 손실 한도가 아니다.', '',
             '| 방식 | 규칙 | 기간 | 비용 | 운용액 수익 | 전체계좌 수익 | 전체계좌 DD | 전체 최종 BTC |',
             '|---|---|---|---|---:|---:|---:|---:|']
    for row in rows:
        s,w = row['allocation_summary'],row['whole_wallet']
        lines.append(f"| {row['instrument']} | {row['case']} | {row['period']} | {row['cost']} | "
                     f"{s['return_pct']:+.2f}% | {w['total_return_pct']:+.2f}% | "
                     f"{w['total_max_drawdown_pct']:.2f}% | {w['final_total_btc']:.8f} |")
    (output/'comparison.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'comparisons':len(rows),'orders_enabled':False}))
    return combined


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coinm', type=Path, default=ROOT/'btc_lab/state/small_coinm_20260924')
    parser.add_argument('--spot', type=Path, default=ROOT/'btc_lab/state/small_spot_20260924')
    parser.add_argument('--output', type=Path, default=ROOT/'btc_lab/state/small_capital_comparison_20260924')
    args = parser.parse_args()
    build(args.coinm,args.spot,args.output)
