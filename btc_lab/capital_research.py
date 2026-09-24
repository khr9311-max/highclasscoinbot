"""Fixed eight-run sensitivity study at the planned 0.003 BTC allocation.

This is research, not a new selection round or an order-capable program.
Run: python -m btc_lab.capital_research
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

from .engine import Config, run as simulate
from .growth_metrics import growth_metrics
from .research import (ROOT, clean, completed_days, enrich, period_input,
                       read_market, save, target_schedule, ts)

PAPER_CANDIDATE = "momentum60_stop20"
CASES = {"momentum60_stop20": (.60, 2.0), "momentum40_stop20": (.40, 1.25)}
PERIODS = {"continuous_from_20210401": "2021-04-01", "recent_reset_20250101": "2025-01-01"}
INITIAL_BTC, TAKER_FEE, MAINTENANCE_RATE = .003, .0005, .004


def simulation_config(candidate, cost_factor):
    if candidate not in CASES or cost_factor not in (1, 2):
        raise ValueError("Only the two frozen candidates and base/2x costs are supported")
    return Config(initial_btc=INITIAL_BTC, fee=TAKER_FEE * cost_factor,
                  slip_bps=3 * cost_factor, stop_slip_bps=10 * cost_factor,
                  max_exposure=CASES[candidate][1], leverage=3, stop_pct=.20,
                  intrabar_funding_policy="adverse")


def contract_diagnostics(result, bars, targets, spec):
    """Describe indivisible contracts without adding a fractional-contract run.

    Daily unit exposure uses the previous completed close. This is an explicit
    granularity diagnostic, not the exact fill-time sizing constraint.
    """
    curve = {point["t"]: point for point in result["equity_curve"]}
    units, below_minimum = [], 0
    for timestamp, target in targets.items():
        point = curve[timestamp]
        value = point["equity_btc"] * point["mark_price"]
        if value > 0:
            unit = spec.contract_size * spec.qty_step / value
            units.append(unit * 100)
            below_minimum += int(bool(target) and value * abs(target) < spec.contract_size * spec.min_qty)
    maximum = max((abs(event[key]) for event in result["events"] if event["type"] == "fill"
                   for key in ("position_before", "position_after")), default=0.0)
    return {"contract_size_usd": spec.contract_size, "qty_step": spec.qty_step,
            "minimum_contracts": spec.min_qty, "maximum_absolute_contracts": maximum,
            "one_step_exposure_pct_initial": spec.contract_size * spec.qty_step /
                (bars[0].mark_o * result["summary"]["initial_btc"]) * 100,
            "one_step_exposure_pct_daily_median": statistics.median(units) if units else None,
            "one_step_exposure_pct_daily_min": min(units) if units else None,
            "one_step_exposure_pct_daily_max": max(units) if units else None,
            "daily_target_count": len(targets),
            "daily_targets_below_minimum_using_prior_close": below_minimum,
            "engine_below_minimum_count": result["audit"]["below_minimum"],
            "engine_margin_or_exposure_cap_count": result["audit"]["margin_caps"],
            "nonpositive_equity_observed": any(point["equity_btc"] <= 0
                                                for point in result["equity_curve"]),
            "fractional_contract_counterfactual_run": False}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frozen_protocol(output, cache, spec, quality):
    source_files = [Path(__file__), Path(__file__).with_name("engine.py"),
                    Path(__file__).with_name("research.py"), Path(__file__).with_name("growth_metrics.py")]
    evidence_dir = ROOT / "btc_lab/state/preflight_20260924_pc"
    evidence_files = [evidence_dir / name for name in ("account_snapshot.json", "capital_plan.json")]
    protocol = {"created_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Fixed planned-capital sensitivity; not strategy reselection",
        "initial_btc": INITIAL_BTC, "actual_current_taker_fee": TAKER_FEE,
        "current_first_maintenance_bracket": MAINTENANCE_RATE,
        "spec": asdict(spec), "data_quality": quality, "cases": CASES,
        "periods": PERIODS, "cost_factors": {"base": 1, "stress_2x": 2}, "run_count": 8,
        "frozen_paper_candidate": PAPER_CANDIDATE, "selection_from_this_experiment": False,
        "orders_enabled": False, "live_eligible": False,
        "source_sha256": {path.name: _sha(path) for path in source_files},
        "data_sha256": {path.name: _sha(path) for path in sorted(cache.iterdir())
                        if path.name.startswith(("BTCUSD_PERP", "exchange_info_BTCUSD_PERP"))},
        "preflight_evidence_sha256": {path.name: _sha(path) for path in evidence_files if path.exists()},
        "limitations": [
            "The current first maintenance bracket 0.004 is not evidence of historical margin rules.",
            "One constant maintenance rate and a shared BTC wallet approximate account margin and liquidation.",
            "The 0.003 BTC allocation is modeled, not transferred or traded by this program.",
            "Both periods reuse inspected history; the recent period resets to 0.003 BTC independently.",
            "Double costs multiply taker fees and both ordinary/stop slippage, not historical funding rates.",
            "Integer contracts make risk allocation coarse; no fractional-contract counterfactual is added.",
            "Hourly marked drawdown can understate intrahour drawdown, gaps and liquidation depth.",
            "The previously chosen momentum60_stop20 paper candidate is not replaced by these outcomes.",
        ]}
    path = output / "protocol.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        strip = lambda item: {key: value for key, value in clean(item).items() if key != "created_utc"}
        if strip(previous) != strip(protocol):
            raise ValueError("Capital protocol, sources or data changed; use a new output folder")
        return previous
    save(path, protocol)
    return protocol


def run(cache, output):
    cache, output = Path(cache), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    bars, funding, original_spec, quality = read_market(cache)
    spec = replace(original_spec, maint_margin_rate=MAINTENANCE_RATE)
    protocol = _frozen_protocol(output, cache, spec, quality)
    days = completed_days(bars)
    end = bars[-1].t + 3600
    results = {}
    for name, (volatility, cap) in CASES.items():
        targets = target_schedule(days, "momentum_20_60_120", volatility, cap)
        results[name] = {}
        for period, start_date in PERIODS.items():
            period_bars, period_funding, period_targets = period_input(bars, funding, targets, ts(start_date), end)
            if not period_bars or period_bars[0].t != ts(start_date):
                raise ValueError(f"Missing start of fixed research period: {period}")
            results[name][period] = {}
            for cost, factor in (("base", 1), ("stress_2x", 2)):
                cfg = simulation_config(name, factor)
                full = simulate(period_bars, period_funding, period_targets, spec, cfg)
                stats = enrich(full)
                stats["growth"] = growth_metrics(full)
                stats["contracts"] = contract_diagnostics(full, period_bars, period_targets, spec)
                results[name][period][cost] = stats
                save(output / f"{name}_{period}_{cost}.json", full)
                print(json.dumps({"candidate": name, "period": period, "cost": cost,
                    "return_pct": stats["return_pct"], "max_drawdown_pct": stats["max_drawdown_pct"],
                    "max_contracts": stats["contracts"]["maximum_absolute_contracts"],
                    "liquidations": stats["liquidations"], "bankrupt": stats["bankrupt"]}), flush=True)
    report = {"protocol": protocol, "end_utc": datetime.fromtimestamp(end, timezone.utc).isoformat(),
              "results": results, "frozen_paper_candidate": PAPER_CANDIDATE,
              "selection_changed": False, "orders_enabled": False}
    save(output / "report.json", report)
    lines = ["# 실제 배정액 0.003 BTC의 고정 비교", "",
             "수수료 0.0005, 현재 첫 유지증거금 구간 0.004. 기존 momentum60 종이후보 유지.",
             "현재 유지증거금률을 과거 전체에 적용한 상수 모형이며 과거 규정 검증이 아니다.",
             "최근 기간은 2025-01-01에 0.003 BTC로 별도 시작한다. 실주문·자금 이체 없음.", "",
             "| 후보 | 기간 | 비용 | BTC 수익 | 최대낙폭 | 최대 계약 | 청산 | 최종 BTC |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for name, periods in results.items():
        for period, costs in periods.items():
            for cost, stats in costs.items():
                lines.append(f"| {name} | {period} | {cost} | {stats['return_pct']:+.6f}% | "
                             f"{stats['max_drawdown_pct']:.6f}% | {stats['contracts']['maximum_absolute_contracts']:g} | "
                             f"{stats['liquidations']} | {stats['final_btc']:.12f} |")
    lines += ["", "## 정수 계약 제약", "",
              "아래 1계약 노출은 매일 목표 결정 직전 완료 봉 평가액 기준이다. 진입 시점의 정확한 제약과 구분한다.", "",
              "| 후보 | 기간 | 비용 | 1계약 노출 중앙값 | 최소수량 미달(엔진) | 일별 목표 수 |",
              "|---|---|---|---:|---:|---:|"]
    for name, periods in results.items():
        for period, costs in periods.items():
            for cost, stats in costs.items():
                diag = stats["contracts"]
                lines.append(f"| {name} | {period} | {cost} | {diag['one_step_exposure_pct_daily_median']:.2f}% | "
                             f"{diag['engine_below_minimum_count']} | {diag['daily_target_count']} |")
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "binance_coinm_v1/state/cache")
    parser.add_argument("--output", type=Path, default=ROOT / "btc_lab/state/capital_20260924")
    args = parser.parse_args()
    run(args.cache, args.output)
