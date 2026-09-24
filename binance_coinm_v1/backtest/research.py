"""Offline, fixed-candidate research. Never creates orders or validation certificates.

Run: python -m binance_coinm_v1.backtest.research
Uses cached public COIN-M data and explicit defaults; does not load .env.
Candidate rules and selection are written to protocol.json BEFORE simulation.
Historical results have previously been inspected: this is exploratory, not blind OOS.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config.settings import Settings
from .data import Dataset, load_dataset
from .metrics import by_direction, max_drawdown, summarize_run
from .research_signals import CANDIDATES, build_precomputed
from .runner import sim_config
from .simulator import Precomputed, Simulator

PACKAGE = Path(__file__).resolve().parents[1]
BASELINE = "trendy_kangaroo_current"
START = "2020-10-01"
SPLITS = {
    "development": (START, "2024-01-01"),
    "validation": ("2024-01-01", "2025-01-01"),
    "recent": ("2025-01-01", None),
    "bear_2022": ("2022-01-01", "2023-01-01"),
    "full": (START, None),
}


def timestamp(date):
    return datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()


def iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return clean(value.tolist())
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def write_json(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")


class ResearchSimulator(Simulator):
    """Also charge adverse slippage on the artificial period-end liquidation."""

    def _close_part(self, tr, qty, px, ts, reason, cfg):
        if reason == "end_of_data":
            px = self._px(px, "SELL" if tr.direction > 0 else "BUY", cfg.slippage_bps)
        return super()._close_part(tr, qty, px, ts, reason, cfg)


def window(ds, pre, start, end):
    """Keep historical indicators, reset positions/equity, exclude all future bars."""
    lo = int(np.searchsorted(ds.ltf.t, start, side="left"))
    hi = int(np.searchsorted(ds.ltf.t, end, side="left"))
    if hi <= lo:
        raise ValueError("Empty research period")
    p = copy.copy(pre)
    p.ltf = ds.ltf.upto(hi)
    p.n = hi
    p.valid = pre.valid[:hi].copy()
    p.valid[:lo] = False
    for attr in ("long", "short", "exit_long", "exit_short"):
        setattr(p, attr, getattr(pre, attr)[:hi])
    mark = ds.mark.upto(hi) if ds.mark is not None else p.ltf
    funding = [f for f in ds.funding if start <= f.funding_time_ms / 1000 < end]
    return ResearchSimulator(p.ltf, mark, funding, ds.spec, p)


def describe_result(ds, result, start, end):
    s = summarize_run(result)
    initial = result.config.start_equity_btc
    idx = np.searchsorted(ds.ltf.t, result.equity_ts)
    if not len(idx):
        raise ValueError("No valid bars in research period")
    marks = ds.mark if ds.mark is not None else ds.ltf
    start_price = float(marks.o[idx[0]])
    prices = marks.c[idx]
    btc_curve = np.r_[initial, result.equity_curve]
    usd_curve = np.r_[initial * start_price, result.equity_curve * prices]
    s["max_drawdown"] = max_drawdown(btc_curve)[0]
    s["usd_total_return_mark_proxy"] = float(usd_curve[-1] / usd_curve[0] - 1)
    s["usd_max_drawdown_mark_proxy"] = max_drawdown(usd_curve)[0]
    s["btc_hold_usd_return"] = float(prices[-1] / start_price - 1)
    s["btc_hold_usd_max_drawdown"] = max_drawdown(np.r_[start_price, prices])[0]
    s["usd_excess_vs_btc_hold_percentage_points"] = 100 * (
        s["usd_total_return_mark_proxy"] - s["btc_hold_usd_return"])
    years = (end - start) / (365.25 * 86400)
    s["cagr_btc"] = (result.final_equity / initial) ** (1 / years) - 1
    s["trades_per_30_days"] = len(result.trades) / ((end - start) / (30 * 86400))
    s["by_direction"] = by_direction(result.trades)
    s["period_start"] = iso(start)
    s["period_end_exclusive"] = iso(end)
    s["valuation_start_price"] = start_price
    s["valuation_end_price"] = float(prices[-1])
    # Ledger conservation catches omitted fees/funding in the offline simulator.
    if not math.isclose(initial + sum(t["net_btc"] for t in result.trades),
                        result.final_equity, rel_tol=1e-10, abs_tol=1e-12):
        raise AssertionError("Research ledger does not reconcile")
    return s


def choose_on_development(summaries):
    """Predeclared BTC trading-edge screen; wallet USD drawdown is reported separately.

    A held BTC is the common collateral benchmark. Ranking USD total return would
    confound its appreciation with strategy skill, especially across dates.
    """
    eligible = [(name, s) for name, s in summaries.items()
                if s["n"] >= 30 and s["total_return"] > 0 and s["max_drawdown"] <= 0.15]
    if not eligible:
        return None
    return max(eligible, key=lambda x: (x[1]["cagr_btc"] / max(x[1]["max_drawdown"], 1e-9),
                                       x[0]))[0]


def selection_report(rows):
    account = {name: data["account"] for name, data in rows.items()}
    winner = choose_on_development({n: d["development"]["base"] for n, d in account.items()})
    if winner is None:
        return {"development_winner": None, "passed_validation_screen": False,
                "recent_result_consulted_for_selection": False, "live_eligible": False,
                "decision": "No candidate: development screen failed; no strategy promotion."}
    val = account[winner]["validation"]
    passed = all(val[c]["n"] >= 30 and val[c]["total_return"] > 0
                 and val[c]["max_drawdown"] <= 0.15 for c in ("base", "stress_2x"))
    return {"development_winner": winner, "passed_validation_screen": passed,
            "decision": ("Candidate for further forward paper research only." if passed else
                         "Development winner failed validation; do not replace with recent winner."),
            "recent_result_consulted_for_selection": False,
            "live_eligible": False}


def run(data_state, output, account_equity=0.007, log=print):
    if not math.isfinite(account_equity) or account_equity <= 0:
        raise ValueError("account_equity must be finite and positive")
    output = Path(output)
    if output.resolve() == Path(data_state).resolve():
        raise ValueError("Research output must use a separate folder from operating state")
    output.mkdir(parents=True, exist_ok=True)
    ds = load_dataset(str(data_state), "BTCUSD_PERP")
    settings = Settings.build()  # Explicit defaults, no credentials or .env.
    last = float(ds.ltf.t[-1]) + ds.ltf.period
    if last <= timestamp("2025-01-01"):
        raise ValueError("Recent evaluation data missing")
    base_cfg = sim_config(settings, settings.exit_mode, settings.directions, 1.0)
    protocol = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Exploratory offline comparison; not maximum-profit optimization or live approval",
        "historical_data_previously_viewed": True,
        "candidates": [{**asdict(c), "max_hold_bars": c.max_hold_bars} for c in CANDIDATES],
        "rule_definitions": {
            "donchian": "Close breaks prior 20-bar high/low; exit beyond opposite prior 10-bar channel.",
            "ema_cross": "EMA20/EMA80 cross, initialized causally from first close; opposite-cross exit.",
            "bollinger_reentry": "Prior close outside its 20-bar 2-SD band, current close reenters current band; mean exit.",
            "common": "2 ATR14 initial stop; full target at declared price R; next-hour recross entry; 1h expiry for all new candidates.",
        },
        "source_sha256": {str(p.relative_to(PACKAGE)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (Path(__file__), Path(__file__).with_name("research_signals.py"),
                                    Path(__file__).with_name("simulator.py"),
                                    PACKAGE / "strategy" / "ladder.py", PACKAGE / "risk" / "sizing.py")},
        "baseline": BASELINE,
        "previous_exit_direction_trials": 15,
        "new_rule_trials": len(CANDIDATES),
        "known_rule_trials_at_least": 15 + len(CANDIDATES),
        "equity_cases": {"reference": 1.0, "account": account_equity},
        "splits": SPLITS,
        "base_execution_config": asdict(base_cfg),
        "selection": "Account development only: >=30 trades, BTC return>0, BTC MDD<=15%; "
                     "highest BTC CAGR/MDD. Reject if validation base OR 2x costs has <30 trades, "
                     "return<=0 or MDD>15%. No fallback to another candidate. Never uses recent results.",
        "cash_benchmark": "No futures trades means constant BTC, NOT constant KRW/USD.",
        "limitations": [
            "Already viewed history is not a genuinely blind holdout; forward paper is still required.",
            "All periods reset equity/positions and liquidate at period end; no cross-boundary PnL leakage.",
            "Signals at closed bars; next-hour recross entry with 1h expiry, not guaranteed next-open fill.",
            "2x cost stress doubles taker fee and both slippages; position sizing recalculates risk budget.",
            "USD wallet uses COIN-M mark-price proxy. Historical USD/KRW is unavailable: no KRW return claimed.",
            "OHLC stop-first assumptions; no queue/depth/latency/liquidation/ADL/outage replay.",
            "Funding marks missing in older records use hourly mark open; tiny funding-time offsets are bucketed.",
            "Current contract specification snapshot and assumed fees, not historical fee-tier replay.",
            "No new DSR/PBO certificate: previous 15-trial metrics do not validate these additional rules.",
        ],
        "data": {"first": iso(ds.ltf.t[0]), "end": iso(last), "bars_1h": len(ds.ltf),
                 "mark_missing": ds.mark_missing, "funding_records": len(ds.funding),
                 "funding_missing_mark": sum(f.mark_price is None for f in ds.funding),
                 "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((Path(data_state) / "cache").iterdir()) if p.is_file()}},
    }
    write_json(output / "protocol.json", protocol)
    rows = {}
    configs = {c.name: replace(base_cfg, variant=c.variant, valid_bars=c.valid_bars,
                              max_hold_bars=c.max_hold_bars) for c in CANDIDATES}
    configs[BASELINE] = base_cfg
    names = list(configs)
    for name in names:
        log(f"Precompute {name}", flush=True)
        if name == BASELINE:
            pre = Precomputed(ds.ltf, ds.htf, settings.zone_bars, settings.min_rr, (1, -1),
                              progress=lambda i, n: log(f"  baseline {i}/{n}", flush=True))
        else:
            pre = build_precomputed(ds, next(c for c in CANDIDATES if c.name == name))
        rows[name] = {"signals": pre.signal_count()}
        for case, equity in (("reference", 1.0), ("account", account_equity)):
            rows[name][case] = {}
            for period, (begin, finish) in SPLITS.items():
                start, end = timestamp(begin), timestamp(finish) if finish else last
                sim = window(ds, pre, start, end)
                rows[name][case][period] = {}
                for costs, factor in (("base", 1), ("stress_2x", 2)):
                    cfg = replace(configs[name], start_equity_btc=equity,
                                  taker_fee=base_cfg.taker_fee * factor,
                                  slippage_bps=base_cfg.slippage_bps * factor,
                                  stop_slippage_bps=base_cfg.stop_slippage_bps * factor)
                    result = sim.run(cfg)
                    summary = describe_result(ds, result, start, end)
                    rows[name][case][period][costs] = summary
                    if case == "account" and costs == "base":
                        write_json(output / f"{name}_{period}_trades.json", result.trades)
                        # Store daily sampled curves as a compact audit artifact.
                        days = (result.equity_ts // 86400).astype(int)
                        ix = np.r_[np.flatnonzero(np.diff(days)), len(days) - 1]
                        with (output / f"{name}_{period}_daily.csv").open("w", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            writer.writerow(["bar_close_utc", "equity_btc", "equity_usd_mark_proxy"])
                            for k in ix:
                                j = int(np.searchsorted(ds.ltf.t, result.equity_ts[k]))
                                mk = ds.mark if ds.mark is not None else ds.ltf
                                writer.writerow([iso(result.equity_ts[k] + ds.ltf.period),
                                                 result.equity_curve[k], result.equity_curve[k] * mk.c[j]])
            log(f"  {case}: recent BTC {rows[name][case]['recent']['base']['total_return']:+.2%}", flush=True)
        # Retain completed work during a long run.
        write_json(output / "partial_results.json", rows)
    report = {"protocol": protocol, "results": rows, "selection": selection_report(rows)}
    write_json(output / "research_results.json", report)
    write_tables(output, report)
    log(json.dumps(report["selection"], ensure_ascii=False), flush=True)
    return report


def write_tables(output, report):
    lines = ["# 고정 후보 전략 비교", "", "연구용 결과이며 실거래 검증 통과를 뜻하지 않습니다.", "",
             "0.007 BTC 기준은 기본 실행값입니다. 실제 실행 시작자산은 protocol.json을 확인하세요.", "",
             "각 기간 시작 시 동일 자산으로 초기화. BTC 수익은 매매 성과, USD는 담보 가격 변동 포함.", ""]
    for period in SPLITS:
        lines += [f"## {period}", "", "| 전략 | 거래 수 | BTC 수익 | BTC MDD | USD 총수익* | USD MDD* | 비용 2배 BTC 수익 | 월 거래 수 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for name, item in report["results"].items():
            s, stress = item["account"][period]["base"], item["account"][period]["stress_2x"]
            lines.append(f"| {name} | {s['n']} | {s['total_return']:+.2%} | {s['max_drawdown']:.2%} | "
                         f"{s['usd_total_return_mark_proxy']:+.2%} | {s['usd_max_drawdown_mark_proxy']:.2%} | "
                         f"{stress['total_return']:+.2%} | {s['trades_per_30_days']:.2f} |")
        lines += ["", f"BTC 단순보유 USD 수익: {s['btc_hold_usd_return']:+.2%}, "
                  f"USD MDD: {s['btc_hold_usd_max_drawdown']:.2%}.", ""]
    lines += ["* USD는 마크가격 평가 근사이며 원화 환율 변동은 포함하지 않음.", "",
              "## 사전 선택 규칙 결과", "", json.dumps(report["selection"], ensure_ascii=False), "",
              "후보 정의·제약·데이터 해시: protocol.json. 상세 수치: research_results.json."]
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-state", type=Path, default=PACKAGE / "state")
    parser.add_argument("--output", type=Path, default=PACKAGE / "state" / "strategy_research_20260924")
    parser.add_argument("--account-equity", type=float, default=0.007)
    args = parser.parse_args()
    run(args.data_state, args.output, args.account_equity)


if __name__ == "__main__":
    main()
