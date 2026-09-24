"""Fixed BTC EMA timeframe study, offline only; never reads .env or sends orders.

Run: python -m binance_coinm_v1.backtest.timeframe_research
The protocol and input hashes are frozen before any candidate is simulated.
Previously viewed history is exploratory evidence, not a blind holdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config.settings import Settings
from ..strategy.price_action import Bars, atr
from ..strategy.signals import TradeSignal
from .data import Dataset, load_dataset
from .research import (PACKAGE, clean, describe_result, iso, selection_report, timestamp,
                       window, write_json)
from .research_signals import ResearchPrecomputed, _ema
from .runner import sim_config


@dataclass(frozen=True)
class TimeframeCandidate:
    source_hours: int
    filter_hours: int | None = None
    warmup_bars: int = 200
    target_r: float = 3.0
    atr_bars: int = 14
    atr_stop_multiple: float = 2.0

    @property
    def name(self) -> str:
        suffix = f"_filter_{self.filter_hours}h" if self.filter_hours else ""
        return f"ema_cross_{self.source_hours}h{suffix}"

    @property
    def max_hold_bars(self) -> int:
        return 72 * self.source_hours


CANDIDATES = tuple(TimeframeCandidate(h) for h in (1, 2, 4, 6, 12, 24)) + (
    TimeframeCandidate(1, 4), TimeframeCandidate(1, 24), TimeframeCandidate(4, 24),
)
SPLITS = {
    "development": ("2021-04-01", "2024-01-01"),
    "validation": ("2024-01-01", "2025-01-01"),
    "recent": ("2025-01-01", None),
    "full": ("2021-04-01", None),
}


def aggregate_complete_utc(bars: Bars, hours: int) -> Bars:
    """Aggregate only full, contiguous UTC buckets of closed 1h candles.

    A leading/trailing partial bucket or any bucket with a missing hour is
    discarded. No interpolation, future fill, or fabricated sub-hour data.
    """
    if bars.period != 3600 or not isinstance(hours, int) or hours < 1:
        raise ValueError("Requires 1h input and positive integer source hours")
    if any(len(getattr(bars, k)) != len(bars) for k in ("t", "o", "h", "l", "v")):
        raise ValueError("Bar array lengths differ")
    if len(bars) and (not np.isfinite(bars.t).all() or
                      np.any(bars.t % 3600 != 0) or np.any(np.diff(bars.t) <= 0)):
        raise ValueError("Input times must be unique, increasing, UTC-aligned hours")
    period = hours * 3600
    buckets = np.floor_divide(bars.t, period) * period
    starts = np.r_[0, np.flatnonzero(np.diff(buckets)) + 1, len(bars)]
    rows = []
    for a, b in zip(starts[:-1], starts[1:]):
        if b - a != hours:
            continue
        start = float(buckets[a])
        if not np.array_equal(bars.t[a:b], start + np.arange(hours) * 3600):
            continue
        rows.append((start, float(bars.o[a]), float(bars.h[a:b].max()),
                     float(bars.l[a:b].min()), float(bars.c[b - 1]),
                     float(bars.v[a:b].sum())))
    return Bars.from_rows(rows, period)


def closed_filter_direction(source: Bars, close_times: np.ndarray, warmup: int = 200):
    """Direction of the last fully closed HTF candle; zero until warmup/equality."""
    direction = np.zeros(len(close_times), dtype=np.int8)
    ready = np.zeros(len(close_times), dtype=bool)
    if not len(source):
        return direction, ready
    indices = np.searchsorted(source.t + source.period, close_times, side="right") - 1
    ready = indices >= warmup
    fast, slow = _ema(source.c, 20), _ema(source.c, 80)
    direction[ready] = np.sign(fast[indices[ready]] - slow[indices[ready]]).astype(np.int8)
    return direction, ready


def build_precomputed(ds: Dataset, candidate: TimeframeCandidate) -> ResearchPrecomputed:
    if candidate not in CANDIDATES:
        raise ValueError("Only the nine prespecified timeframe candidates are supported")
    source = aggregate_complete_utc(ds.ltf, candidate.source_hours)
    pre = ResearchPrecomputed(ds.ltf)
    if not len(source) or not pre.n:
        return pre
    base_close = ds.ltf.t + ds.ltf.period
    source_close = source.t + source.period
    pre.valid[:] = np.searchsorted(source_close, base_close, side="right") > candidate.warmup_bars
    filter_direction = None
    if candidate.filter_hours:
        htf = aggregate_complete_utc(ds.ltf, candidate.filter_hours)
        filter_direction, ready = closed_filter_direction(htf, base_close, candidate.warmup_bars)
        pre.valid &= ready
    fast, slow = _ema(source.c, 20), _ema(source.c, 80)
    for j in range(candidate.warmup_bars, len(source)):
        closed_at = float(source_close[j])
        i = int(np.searchsorted(base_close, closed_at))
        if i >= pre.n:
            break
        if float(base_close[i]) != closed_at:
            continue
        long_cross = bool(fast[j] > slow[j] and fast[j - 1] <= slow[j - 1])
        short_cross = bool(fast[j] < slow[j] and fast[j - 1] >= slow[j - 1])
        # Exit remains the lower-timeframe opposite cross, even if the filter
        # would block a new opposite entry. Filter changes never create entries.
        pre.exit_long[i], pre.exit_short[i] = short_cross, long_cross
        if not pre.valid[i] or not (long_cross or short_cross):
            continue
        direction = 1 if long_cross else -1
        if filter_direction is not None and filter_direction[i] != direction:
            continue
        close = float(source.c[j])
        volatility = atr(source, j, candidate.atr_bars)
        distance = candidate.atr_stop_multiple * volatility
        stop = close - direction * distance
        target = close + direction * candidate.target_r * distance
        if (not np.isfinite([close, volatility, distance, stop, target]).all()
                or distance <= 0 or min(close, stop, target) <= 0):
            continue
        signal = TradeSignal(
            candidate.name, direction, i, float(source.t[j]), closed_at,
            close, stop, [target], volatility,
            {"source_period_hours": float(candidate.source_hours),
             "source_bar_index": float(j), "target_r": candidate.target_r,
             **({"filter_period_hours": float(candidate.filter_hours)}
                if candidate.filter_hours else {})},
        )
        (pre.long if direction > 0 else pre.short)[i] = signal
    return pre


def freeze_protocol(path: Path, protocol):
    """Permit reproducibility reruns, but never silently replace frozen rules/data."""
    protocol = clean(protocol)
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        comparable = lambda p: {k: v for k, v in p.items() if k != "created_at"}
        if comparable(previous) != comparable(protocol):
            raise ValueError("Frozen research protocol changed: use a new output folder")
        return previous
    write_json(path, protocol)
    return protocol


def run(data_state, output, account_equity=0.007, log=print):
    if not math.isfinite(account_equity) or account_equity <= 0:
        raise ValueError("Account equity must be finite and positive")
    output, data_state = Path(output), Path(data_state)
    if output.resolve() == data_state.resolve():
        raise ValueError("Research output must differ from operating state")
    ds = load_dataset(str(data_state), "BTCUSD_PERP")
    last = float(ds.ltf.t[-1]) + ds.ltf.period
    if last <= timestamp("2025-01-01"):
        raise ValueError("Recent evaluation data missing")
    settings = Settings.build()  # No .env and no private API credentials.
    base_cfg = sim_config(settings, "zone", (1, -1), 1.0)
    base_cfg = replace(base_cfg, valid_bars=1)
    source_files = (Path(__file__), Path(__file__).with_name("research.py"),
                    Path(__file__).with_name("research_signals.py"),
                    Path(__file__).with_name("simulator.py"),
                    PACKAGE / "strategy" / "ladder.py", PACKAGE / "risk" / "sizing.py")
    protocol = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "BTC quantity after fees/funding; exploratory offline timeframe comparison",
        "historical_data_previously_viewed": True,
        "candidates": [{**asdict(c), "name": c.name, "max_hold_bars": c.max_hold_bars}
                       for c in CANDIDATES],
        "rules": {"entry": "EMA20/80 cross on a closed source candle; next-1h recross, 1h expiry",
                  "filter": "Last closed HTF EMA20>80 allows long; EMA20<80 allows short; equality blocks both",
                  "exit": "Unfiltered opposite source cross, 2 ATR14 stop, 3R full target, or 72 source bars",
                  "warmup": "200 prior complete candles for both source and optional filter",
                  "aggregation": "1h OHLCV to complete contiguous UTC buckets; no partial candles"},
        "previous_known_rule_trials": 21, "new_rule_trials": 7,
        "known_rule_trials_at_least": 28,
        "reused_rules": ["ema_cross_1h", "ema_cross_4h"],
        "equity_cases": {"reference": 1.0, "account": account_equity},
        "splits": {name: list(span) for name, span in SPLITS.items()},
        "base_execution_config": asdict(base_cfg),
        "selection": "Account development only: >=30 trades, BTC return>0, BTC MDD<=15%; "
                     "highest BTC CAGR/MDD. Validation base AND 2x costs each require >=30 trades, "
                     "return>0 and MDD<=15%. No fallback candidate. Recent results excluded from selection.",
        "data": {"first": iso(ds.ltf.t[0]), "end_exclusive": iso(last),
                 "bars_1h": len(ds.ltf), "mark_missing": ds.mark_missing,
                 "funding_records": len(ds.funding),
                 "funding_missing_mark": sum(f.mark_price is None for f in ds.funding),
                 "complete_bars_by_hours": {str(h): len(aggregate_complete_utc(ds.ltf, h))
                                             for h in (1, 2, 4, 6, 12, 24)},
                 "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((data_state / "cache").iterdir()) if p.is_file()}},
        "source_sha256": {str(p.relative_to(PACKAGE)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in source_files},
        "limitations": [
            "15m and lower cannot be reconstructed from 1h candles; original subhour data not obtained.",
            "Viewed history is not a blind holdout. No production strategy or validation gate changed.",
            "Each period resets positions/equity and closes at its end with adverse slippage.",
            "2x fee and entry/stop slippage stress also recalculates affordable position size.",
            "Small account integer contract constraints and skipped entries are included.",
            "Missing historic funding marks use hourly mark open; current contract specs/fees are assumed.",
            "No order book, queue, latency, liquidation, ADL, or exchange outage replay.",
            "BTC is the performance objective; USD mark proxy includes collateral price changes, not KRW FX.",
            "More candidate comparisons raise overfitting risk; no new DSR/PBO or live certificate claimed.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    protocol = freeze_protocol(output / "protocol.json", protocol)
    rows = {}
    for candidate in CANDIDATES:
        log(f"Precompute {candidate.name}", flush=True)
        pre = build_precomputed(ds, candidate)
        rows[candidate.name] = {"signals": pre.signal_count()}
        for case, equity in (("reference", 1.0), ("account", account_equity)):
            results = rows[candidate.name][case] = {}
            for period, (begin, finish) in SPLITS.items():
                start, end = timestamp(begin), timestamp(finish) if finish else last
                sim = window(ds, pre, start, end)
                results[period] = {}
                for costs, factor in (("base", 1), ("stress_2x", 2)):
                    cfg = replace(base_cfg, start_equity_btc=equity,
                                  max_hold_bars=candidate.max_hold_bars,
                                  taker_fee=base_cfg.taker_fee * factor,
                                  slippage_bps=base_cfg.slippage_bps * factor,
                                  stop_slippage_bps=base_cfg.stop_slippage_bps * factor)
                    result = sim.run(cfg)
                    results[period][costs] = describe_result(ds, result, start, end)
                    if case == "account" and costs == "base":
                        write_json(output / f"{candidate.name}_{period}_trades.json", result.trades)
            log(f"  {case} recent BTC {results['recent']['base']['total_return']:+.2%}", flush=True)
        write_json(output / "partial_results.json", rows)
    report = {"protocol": protocol, "results": rows, "selection": selection_report(rows)}
    write_json(output / "research_results.json", report)
    write_tables(output, report)
    log(json.dumps(report["selection"], ensure_ascii=False), flush=True)
    return report


def write_tables(output, report):
    equity = report["protocol"]["equity_cases"]["account"]
    lines = ["# BTC 시간봉 고정 후보 비교", "", f"시작 자산 {equity:g} BTC. 수수료·펀딩비 차감 후 BTC 수익률.", "",
             "각 기간은 포지션·자산 초기화. 최근 결과를 보고 후보를 바꾸지 않았으며 실거래 적격 판정이 아니다.", ""]
    for period in SPLITS:
        lines += [f"## {period}", "", "| 후보 | 거래 수 | BTC 수익 | BTC MDD | 비용 2배 수익 | 월 거래 수 |",
                  "|---|---:|---:|---:|---:|---:|"]
        for name, item in report["results"].items():
            s, stress = item["account"][period]["base"], item["account"][period]["stress_2x"]
            lines.append(f"| {name} | {s['n']} | {s['total_return']:+.2%} | {s['max_drawdown']:.2%} | "
                         f"{stress['total_return']:+.2%} | {s['trades_per_30_days']:.2f} |")
        lines.append("")
    lines += ["## 사전에 정한 선택 규칙 결과", "", json.dumps(report["selection"], ensure_ascii=False), "",
              "1시간 원본으로 15분 이하 봉은 만들 수 없어 이번 비교에서 제외했다. 상위봉은 UTC 마감된 완전봉만 사용했다.",
              "기존 21개에 새 규칙 7개를 더해 알려진 규칙 비교는 최소 28개다. 과거 자료는 이미 보았으므로 탐색 결과다.",
              "실행 규칙·소스/데이터 해시: protocol.json. 상세 지표·1 BTC 기준 결과: research_results.json."]
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-state", type=Path, default=PACKAGE / "state")
    parser.add_argument("--output", type=Path, default=PACKAGE / "state" / "timeframe_research_20260924")
    parser.add_argument("--account-equity", type=float, default=0.007)
    args = parser.parse_args()
    run(args.data_state, args.output, args.account_equity)


if __name__ == "__main__":
    main()
