"""Post-hoc fixed-policy capital sensitivity; public cached data, no orders."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

from . import growth_metrics, market_fit, small_spot


ROOT = Path(__file__).resolve().parents[1]
BUDGETS = ("0.0027", "0.0033")
WHOLE_INITIAL = Decimal("0.00412273")
PERIODS = {"continuous_full": "2021-04-01", "recent_reset_2025": "2025-01-01"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(output):
    output = Path(output)
    market = ROOT / "btc_lab/state/small_capital_market"
    hourly = market / "BTCUSDT_1h_spot.csv"
    daily = market / "BTCUSDT_1d_spot.csv"
    quality_path = market / "spot_data_manifest.json"
    symbol = ROOT / "btc_lab/state/market_fit_20260924/exchange_info_BTCUSDT_spot.json"
    original_protocol = ROOT / "btc_lab/state/small_spot_20260924/protocol.json"
    frozen = json.loads(original_protocol.read_text(encoding="utf-8"))
    sources = [Path(__file__), Path(small_spot.__file__), Path(market_fit.__file__), Path(growth_metrics.__file__)]
    source_hashes = {path.name: digest(path) for path in sources}
    for name, expected in frozen["source_files"].items():
        if source_hashes.get(name) != expected:
            raise ValueError("Original spot study source has changed")
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if quality.get("symbol") != "BTCUSDT":
        raise ValueError("Wrong data symbol")
    for interval, path in (("1h", hourly), ("1d", daily)):
        if digest(path) != quality["datasets"][interval]["data_sha256"]:
            raise ValueError("Spot input hash mismatch")
    for path in (hourly, daily, symbol, quality_path):
        if digest(path) != frozen["data_files"][path.name]:
            raise ValueError("Original spot study data has changed")
    bars, days = small_spot.read_spot_csv(hourly), small_spot.read_spot_csv(daily, period_sec=86400)
    filters = small_spot.read_filters(symbol)
    protocol = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Post-hoc +/-10% capital fragility diagnostic, not an independent strategy validation or amount selection",
        "original_results_already_viewed": True,
        "policy": "momentum_fraction", "no_trade_band": 0, "initial_btc_values": BUDGETS,
        "periods": PERIODS, "costs": {"base": {"fee_rate": .001, "slippage_bps": 3},
                                      "stress_2x": {"fee_rate": .002, "slippage_bps": 6}},
        "fee_asset_mode": "received", "whole_initial_btc": str(WHOLE_INITIAL),
        "reserve_rule": "Whole initial BTC minus allocated BTC remains constant BTC; no reserve spending or earnings",
        "selection": "No selection of the better starting amount, strategy change, paper change or live promotion",
        "source_files": source_hashes,
        "data_files": {path.name: digest(path) for path in (hourly, daily, symbol, quality_path)},
        "original_protocol_sha256": digest(original_protocol),
        "data_quality": quality,
        "limitations": frozen["limitations"] + [
            "Sensitivity requested after original results; cannot be described as a blind test.",
            "Whole-account drawdowns assume reserve BTC never changes and omit exchange custody losses.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = output / "protocol.json"
    if protocol_path.exists():
        previous = json.loads(protocol_path.read_text(encoding="utf-8"))
        strip = lambda value: {k: v for k, v in small_spot.clean(value).items() if k != "created_at"}
        if strip(previous) != strip(protocol):
            raise ValueError("Sensitivity protocol changed; use a new output directory")
        protocol = previous
    else:
        small_spot.save(protocol_path, protocol)
    # All eight combinations are frozen above, before deriving targets or returns.
    targets = small_spot.target_schedule(days, "momentum_fraction")
    rows = {}
    for budget_text in BUDGETS:
        budget = Decimal(budget_text)
        reserve = WHOLE_INITIAL - budget
        rows[budget_text] = {}
        for period, begin in PERIODS.items():
            lo, hi = small_spot.stamp(begin), bars[-1].t + 3600
            subset = [bar for bar in bars if lo <= bar.t < hi]
            schedule = {t: weight for t, weight in targets.items() if lo <= t < hi}
            rows[budget_text][period] = {}
            for cost, settings in protocol["costs"].items():
                result = small_spot.simulate_spot(subset, schedule, filters,
                    small_spot.SpotConfig(initial_btc=float(budget), **settings))
                allocated_growth = growth_metrics.growth_metrics(result)
                whole_curve = [{"t": point["t"], "equity_btc": point["equity_btc"] + float(reserve)}
                               for point in result["equity_curve"]]
                whole_growth = growth_metrics.growth_metrics({"equity_curve": whole_curve})
                years = (whole_curve[-1]["t"] - whole_curve[0]["t"]) / (365.25 * 86400)
                final = whole_curve[-1]["equity_btc"]
                whole_summary = {"initial_btc": float(WHOLE_INITIAL), "reserve_btc": float(reserve),
                                 "final_btc": final, "return_pct": whole_growth["cumulative_return_pct"],
                                 "max_drawdown_pct": whole_growth["max_drawdown_pct"],
                                 "cagr_pct": math.expm1(math.log(final / float(WHOLE_INITIAL)) / years) * 100,
                                 "final_held_btc": result["summary"]["final_held_btc"] + float(reserve)}
                result["allocated_growth"] = allocated_growth
                result["whole_account"] = {"summary": whole_summary, "growth": whole_growth,
                                            "equity_curve": whole_curve}
                rows[budget_text][period][cost] = {"allocated": result["summary"],
                                                  "allocated_growth": allocated_growth,
                                                  "whole": whole_summary, "whole_growth": whole_growth}
                small_spot.save(output / f"momentum_fraction_{budget_text}_{period}_{cost}.json", result)
                print(budget_text, period, cost, "complete", flush=True)
    report = {"protocol": protocol, "results": rows, "selected": None, "live_eligible": False}
    small_spot.save(output / "report.json", report)
    lines = ["# 현물 분할 모멘텀 자금 ±10% 민감도", "",
             "기존 결과를 본 뒤 실시한 진단이다. 예산이나 전략을 선택하지 않으며 실거래 승인을 뜻하지 않는다.", "",
             "| 시작 BTC | 기간 | 비용 | 할당 BTC 수익 | 할당 MDD | 전체 BTC 수익 | 전체 MDD |",
             "|---:|---|---|---:|---:|---:|---:|"]
    for budget, periods in rows.items():
        for period, costs in periods.items():
            for cost, row in costs.items():
                a, w = row["allocated"], row["whole"]
                lines.append(f"| {budget} | {period} | {cost} | {a['return_pct']:+.2f}% | "
                             f"{a['max_drawdown_pct']:.2f}% | {w['return_pct']:+.2f}% | {w['max_drawdown_pct']:.2f}% |")
    lines += ["", "전체 계좌는 최초 0.00412273 BTC이며 할당하지 않은 BTC는 일정하게 보유한다고 가정했다.",
              "현재 수량 규칙과 수수료를 과거에도 적용했다. USDT 평가액과 최종 보유 BTC는 JSON에서 별도로 제공한다."]
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "btc_lab/state/small_spot_sensitivity_20260924")
    run(parser.parse_args().output)
