"""Fixed, self-financing BTC/USDT spot research at 0.003 BTC.

Reads actual BTCUSDT spot candles and public symbol filters. No credentials,
exchange order client, borrowing, futures, funding, or liquidation are used.
Every policy starts with BTC only. Uninvested USDT is valued in BTC, so being
in cash while BTC rises reduces BTC wealth even if USDT balances are unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path

from .growth_metrics import growth_metrics


ROOT = Path(__file__).resolve().parents[1]
TERMINAL_MAX_ORDERS = 1000
CASES = (
    ("hold_btc", "hold_btc", 0.0),
    ("momentum_fraction", "momentum_fraction", 0.0),
    ("ema20_100_binary", "ema20_100_binary", 0.0),
    ("momentum_fraction_band05", "momentum_fraction", 0.05),
)
PERIODS = {
    "training_through_2024": ("2021-04-01", "2025-01-01"),
    "recent_reset_2025": ("2025-01-01", None),
    "continuous_full": ("2021-04-01", None),
}


def dec(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid decimal input") from None
    if not result.is_finite():
        raise ValueError("Non-finite financial input")
    return result


def stamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite research result")
    return value


def save(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, allow_nan=False, indent=1), encoding="utf-8")


@dataclass(frozen=True)
class SpotBar:
    t: int
    o: float
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class SpotConfig:
    initial_btc: float = .003
    fee_rate: float = .001
    slippage_bps: float = 3
    no_trade_band: float = 0
    fee_asset_mode: str = "received"

    def __post_init__(self):
        for name in ("initial_btc", "fee_rate", "slippage_bps", "no_trade_band"):
            if isinstance(getattr(self, name), bool):
                raise ValueError("Boolean is not a financial parameter")
            dec(getattr(self, name))
        if self.initial_btc <= 0 or not 0 <= self.fee_rate < 1 or not 0 <= self.slippage_bps < 10000:
            raise ValueError("Invalid spot cost or starting balance")
        if not 0 <= self.no_trade_band <= 1 or self.fee_asset_mode not in ("received", "quote"):
            raise ValueError("Invalid band or commission asset assumption")


def validate_bars(bars, period_sec=3600, require_contiguous=False):
    if not bars:
        raise ValueError("Spot candles are required")
    previous = None
    for bar in bars:
        if isinstance(bar.t, bool) or bar.t != int(bar.t) or bar.t % period_sec:
            raise ValueError("Spot candle timestamps must match their UTC interval")
        if previous is not None and (bar.t <= previous or (require_contiguous and bar.t - previous != period_sec)):
            raise ValueError("Duplicate, reversed or unexpected missing spot intervals")
        previous = bar.t
        if not all(math.isfinite(x) and x > 0 for x in (bar.o, bar.h, bar.l, bar.c)):
            raise ValueError("Invalid spot OHLC values")
        if not bar.l <= min(bar.o, bar.c) <= max(bar.o, bar.c) <= bar.h:
            raise ValueError("Invalid spot OHLC range")


def read_spot_csv(path, now=None, period_sec=3600):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    dec(now)
    out = []
    with Path(path).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            opening = int(row["open_time_ms"])
            if opening % 1000:
                raise ValueError("Unexpected spot candle timestamp precision")
            t = opening // 1000
            if t + period_sec > now:
                raise ValueError("Input contains an unfinished spot candle")
            out.append(SpotBar(t, *(float(row[k]) for k in ("open", "high", "low", "close"))))
    validate_bars(out, period_sec, require_contiguous=period_sec == 86400)
    return out


def completed_days(bars):
    groups = {}
    for bar in bars:
        groups.setdefault(bar.t // 86400 * 86400, []).append(bar)
    return [SpotBar(t, group[0].o, max(x.h for x in group), min(x.l for x in group), group[-1].c)
            for t, group in sorted(groups.items())
            if len(group) == 24 and [x.t for x in group] == [t + i * 3600 for i in range(24)]]


def ema(values, span):
    result = []
    alpha = 2 / (span + 1)
    for value in values:
        result.append(value if not result else alpha * value + (1 - alpha) * result[-1])
    return result


def target_schedule(days, family):
    """Day j close may act only at day j+1 open. No crossing-only entries."""
    if family not in ("hold_btc", "momentum_fraction", "ema20_100_binary"):
        raise ValueError("Only the prespecified spot policies are supported")
    closes = [x.c for x in days]
    fast, slow = ema(closes, 20), ema(closes, 100)
    targets = {}
    for j in range(200, len(days)):
        if any(days[k].t - days[k - 1].t != 86400 for k in range(j - 199, j + 1)):
            continue
        if family == "hold_btc":
            weight = 1.0
        elif family == "momentum_fraction":
            votes = [1 if closes[j] > closes[j - lag] else -1 if closes[j] < closes[j - lag] else 0
                     for lag in (20, 60, 120)]
            weight = (1 + sum(votes) / 3) / 2
        else:
            weight = 1.0 if fast[j] >= slow[j] else 0.0
        targets[days[j].t + 86400] = weight
    return targets


def simulate_spot(bars, targets, filters, config=None):
    """BTC and USDT balances conserve every fill, including received-asset fees.

    At each declared opening the sizing helper solves the after-cost weight and
    floors quantity to the live symbol's step. Orders below current filters are
    skipped. The terminal conversion tries target=1 regardless of the band;
    unconvertible quote dust is reported as value, never as held BTC.
    """
    from .market_fit import size_spot_order
    cfg = config or SpotConfig()
    validate_bars(bars)
    times = {b.t for b in bars}
    if any(isinstance(t, bool) or t != int(t) or t % 3600 or not 0 <= dec(weight) <= 1
           for t, weight in targets.items()):
        raise ValueError("Targets require UTC hour openings and BTC weights in [0,1]")
    unobserved_targets = sorted(t for t in targets if t not in times)
    missing_hours = sum((b.t - a.t) // 3600 - 1 for a, b in zip(bars, bars[1:]))
    btc, quote = dec(cfg.initial_btc), Decimal(0)
    initial = btc
    events, curve = [], []
    skips = Counter()
    fee_btc_total = fee_quote_total = fee_equivalent_btc = turnover_quote = Decimal(0)
    band_skips = terminal_attempts = 0

    def value(price):
        return btc + quote / dec(price)

    def record(timestamp, price):
        net = value(price)
        curve.append({"t": timestamp, "equity_btc": float(net), "held_btc": float(btc),
                      "quote_usdt": float(quote), "price": price,
                      "btc_weight": float(btc / net) if net > 0 else 0.0})

    def rebalance(weight, price, timestamp, reason):
        nonlocal btc, quote, fee_btc_total, fee_quote_total, fee_equivalent_btc, turnover_quote
        nonlocal band_skips, terminal_attempts
        before_btc, before_quote = btc, quote
        before = value(price)
        current_weight = btc / before
        target = dec(weight)
        terminal = reason == "terminal_conversion"
        if terminal:
            terminal_attempts += 1
        elif abs(current_weight - target) < dec(cfg.no_trade_band):
            band_skips += 1
            return False
        result = size_spot_order(btc_balance=btc, quote_balance=quote, target_btc_fraction=target,
                                 bid=dec(price), ask=dec(price), filters=filters,
                                 fee_rate=dec(cfg.fee_rate), slippage_bps=dec(cfg.slippage_bps),
                                 reference_price=dec(price), fee_asset_mode=cfg.fee_asset_mode)
        if result["status"] != "READY":
            skips[str(result["reason"])] += 1
            return False
        quantity, px = dec(result["quantity"]), dec(result["execution_price"])
        btc_delta, quote_delta = dec(result["btc_delta"]), dec(result["quote_delta"])
        fee_btc, fee_quote = dec(result["fee_btc"]), dec(result["fee_quote"])
        if quantity <= 0 or px <= 0 or min(fee_btc, fee_quote) < 0:
            raise AssertionError("Invalid sizing result")
        btc, quote = dec(result["btc_after"]), dec(result["quote_after"])
        if btc < 0 or quote < 0:
            raise AssertionError("Spot simulation cannot borrow")
        if abs(btc - before_btc - btc_delta) > Decimal("1e-20") or abs(quote - before_quote - quote_delta) > Decimal("1e-16"):
            raise AssertionError("Spot wallet deltas do not reconcile")
        if result["side"] == "BUY":
            expected_btc_delta = quantity - fee_btc
            expected_quote_delta = -quantity * px - fee_quote
        elif result["side"] == "SELL":
            expected_btc_delta = -quantity - fee_btc
            expected_quote_delta = quantity * px - fee_quote
        else:
            raise AssertionError("Unexpected spot side")
        if abs(btc_delta - expected_btc_delta) > Decimal("1e-20") or abs(quote_delta - expected_quote_delta) > Decimal("1e-16"):
            raise AssertionError("Fill amounts or commission assets do not reconcile")
        fee_btc_total += fee_btc
        fee_quote_total += fee_quote
        fee_equivalent_btc += fee_btc + fee_quote / dec(price)
        turnover_quote += quantity * px
        events.append({"t": timestamp, "reason": reason, "side": result["side"],
                       "target_btc_fraction": str(target), "quantity": str(quantity), "execution_price": str(px),
                       "reference_price": str(price), "fee_btc": str(fee_btc), "fee_quote_usdt": str(fee_quote),
                       "btc_delta": str(btc_delta), "quote_delta": str(quote_delta),
                       "btc_after": str(btc), "quote_after": str(quote),
                       "equity_change_at_reference_btc": str(value(price) - before)})
        return True

    record(bars[0].t, bars[0].o)
    for bar in bars:
        if bar.t in targets:
            rebalance(targets[bar.t], bar.o, bar.t, "rebalance")
        record(bar.t + 3600, bar.c)
    terminal_price = bars[-1].c
    pre_terminal = {"held_btc": str(btc), "quote_usdt": str(quote), "net_btc": str(value(terminal_price))}
    terminal_status = "not_needed"
    residual_tradeable = False
    if quote > 0:
        terminal_status = "completed"
        for _ in range(TERMINAL_MAX_ORDERS):
            previous_quote = quote
            if not rebalance(1, terminal_price, bars[-1].t + 3600, "terminal_conversion"):
                terminal_status = "residual_not_tradeable_current_filters"
                break
            if quote >= previous_quote:
                raise AssertionError("Terminal BUY must consume quote currency")
            if quote == 0:
                break
        else:
            remaining = size_spot_order(btc_balance=btc, quote_balance=quote, target_btc_fraction=1,
                                        bid=dec(terminal_price), ask=dec(terminal_price), filters=filters,
                                        fee_rate=dec(cfg.fee_rate), slippage_bps=dec(cfg.slippage_bps),
                                        reference_price=dec(terminal_price), fee_asset_mode=cfg.fee_asset_mode)
            residual_tradeable = remaining["status"] == "READY"
            terminal_status = ("order_cap_reached" if residual_tradeable
                               else "residual_not_tradeable_current_filters")
        curve.pop()
        record(bars[-1].t + 3600, terminal_price)
    dust = Decimal(0) if residual_tradeable else quote
    net = value(terminal_price)
    expected_btc = initial + sum((dec(e["btc_delta"]) for e in events), Decimal(0))
    expected_quote = sum((dec(e["quote_delta"]) for e in events), Decimal(0))
    if abs(expected_btc - btc) > Decimal("1e-20") or abs(expected_quote - quote) > Decimal("1e-16"):
        raise AssertionError("Spot ledger does not conserve the initial assets")
    peak, dd = float(initial), 0.0
    for point in curve:
        peak = max(peak, point["equity_btc"])
        dd = max(dd, 1 - point["equity_btc"] / peak)
    years = (bars[-1].t + 3600 - bars[0].t) / (365.25 * 86400)
    ratio = float(net / initial)
    try:
        cagr = math.expm1(math.log(ratio) / years) * 100 if ratio > 0 else -100
    except OverflowError:
        cagr = None
    if cagr is not None and not math.isfinite(cagr):
        cagr = None
    return {
        "summary": {"initial_btc": float(initial), "final_btc": float(net), "final_net_btc": float(net),
                    "final_held_btc": float(btc), "terminal_realizable_btc": float(btc),
                    "quote_dust_usdt": float(dust), "quote_dust_valued_btc": float(dust / dec(terminal_price)),
                    "remaining_quote_usdt": float(quote),
                    "unconverted_tradeable_quote_usdt": float(quote if residual_tradeable else 0),
                    "terminal_residual_tradeable": residual_tradeable,
                    "terminal_conversion_status": terminal_status,
                    "return_pct": (ratio - 1) * 100,
                    "realizable_return_pct": (float(btc / initial) - 1) * 100,
                    "max_drawdown_pct": dd * 100,
                    "drawdown_basis": "Observed hourly closes, initial balance and terminal conversion; excludes unobserved intrahour/gap extremes",
                    "cagr_pct": cagr,
                    "cagr_numerically_available": cagr is not None,
                    "fees_paid_btc": float(fee_btc_total), "fees_paid_usdt": float(fee_quote_total),
                    "fees_equivalent_btc_at_execution": float(fee_equivalent_btc),
                    "turnover_usdt": float(turnover_quote),
                    "turnover_multiple_initial_usdt": float(turnover_quote / (initial * dec(bars[0].o))),
                    "trade_count": len(events), "buy_count": sum(e["side"] == "BUY" for e in events),
                    "sell_count": sum(e["side"] == "SELL" for e in events),
                    "terminal_conversion_attempts": terminal_attempts,
                    "band_skips": band_skips, "skip_reasons": dict(skips),
                    "missing_execution_hours": missing_hours,
                    "unobserved_target_count": len(unobserved_targets),
                    "ledger_btc_error": float(expected_btc - btc), "ledger_usdt_error": float(expected_quote - quote),
                    "start_price": bars[0].o, "end_price": terminal_price},
        "pre_terminal_conversion": pre_terminal, "equity_curve": curve, "events": events,
        "unobserved_target_times": unobserved_targets,
        "config": asdict(cfg),
        "assumptions": [
            "BTCUSDT spot only; portfolio owns BTC and USDT, with no borrowing or derivatives.",
            "Starting balance is BTC only; BTC holding has exactly zero BTC-denominated return.",
            "Completed daily signals execute at the next UTC day opening.",
            "Historical hourly open/close is a quote proxy; actual spreads and order-book depth are unavailable.",
            "Missing hourly records are not filled; their targets are skipped, never delayed to a later observed bar.",
            "Drawdowns use observed hourly closing valuations, not unavailable intrahour or gap valuations.",
            "All trades pay adverse slippage and taker commissions; current symbol filters are used historically.",
            "Received-asset fees: BUY fee in BTC, SELL fee in USDT; BNB discounts not assumed.",
            "The hourly reference price substitutes for any exchange minimum-notional averaging window.",
            "Final USDT-to-BTC conversion obeys the same fees and minimum sizes; quote dust is valued separately.",
            "Net BTC valuation is not the same as BTC already held, especially while invested in USDT.",
        ],
    }


def read_filters(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    symbols = data.get("symbols", [data])
    found = [s for s in symbols if s.get("symbol") == "BTCUSDT"]
    if len(found) != 1 or found[0].get("baseAsset") != "BTC" or found[0].get("quoteAsset") != "USDT":
        raise ValueError("Actual BTCUSDT spot exchangeInfo is required")
    if found[0].get("status") != "TRADING" or not isinstance(found[0].get("filters"), list):
        raise ValueError("BTCUSDT is unavailable or missing filters")
    return found[0]["filters"]


def run_study(candles, exchange_info, output, *, daily_data=None, quality_manifest=None):
    from . import market_fit
    candles, exchange_info, output = Path(candles), Path(exchange_info), Path(output)
    daily_data = Path(daily_data) if daily_data else candles.with_name("BTCUSDT_1d_spot.csv")
    quality_manifest = Path(quality_manifest) if quality_manifest else candles.with_name("spot_data_manifest.json")
    bars, filters = read_spot_csv(candles), read_filters(exchange_info)
    days = read_spot_csv(daily_data, period_sec=86400)
    quality = json.loads(quality_manifest.read_text(encoding="utf-8"))
    if quality.get("symbol") != "BTCUSDT":
        raise ValueError("Spot quality manifest has a different symbol")
    for label, path in (("1h", candles), ("1d", daily_data)):
        if quality["datasets"][label]["data_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Spot data hash does not match quality manifest")
    if bars[-1].t < stamp("2025-01-01"):
        raise ValueError("Recent spot data is missing")
    output.mkdir(parents=True, exist_ok=True)
    protocol = {
        "created_at": datetime.now(timezone.utc).isoformat(), "objective": "Net BTC growth after spot costs at 0.003 BTC",
        "initial_btc": .003, "cases": CASES, "periods": PERIODS,
        "history_previously_viewed": True, "new_fixed_policies": 3, "hold_btc_control": 1,
        "selection": "No ranking or promotion from recent/full results. Fixed comparisons only; live_eligible=false.",
        "signal_definitions": {
            "momentum_fraction": "BTC weight=(1+mean(sign(20/60/120 completed-day returns)))/2. Exact ties contribute sign0.",
            "ema20_100_binary": "BTC weight1 if causal daily EMA20>=EMA100, else USDT weight1.",
            "warmup": "200 earlier completed UTC days; action only next-day opening.",
            "band05": "Skip if absolute target-minus-current BTC weight <0.05; terminal conversion overrides band.",
        },
        "taker_fee": .001, "fee_asset_mode": "received", "slippage_bps": 3,
        "terminal_max_orders": TERMINAL_MAX_ORDERS,
        "stress": "Double fee and slippage, rerun integer-sized portfolios.",
        "fee_basis": "Current BTCUSDT account rate separately checked at0.001; historical constancy is an assumption. No BNB discount.",
        "spec_basis": "Current public spot exchangeInfo filters, not reconstructed historical rules.",
        "filters": filters,
        "data_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (candles, daily_data, exchange_info, quality_manifest)},
        "current_commission_evidence_sha256": (hashlib.sha256(candles.with_name("spot_commissions.json").read_bytes()).hexdigest()
                                               if candles.with_name("spot_commissions.json").exists() else None),
        "data_quality": quality,
        "missing_execution_hours": sum((b.t-a.t)//3600-1 for a,b in zip(bars,bars[1:])),
        "source_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (Path(__file__), Path(market_fit.__file__), Path(__file__).with_name("growth_metrics.py"))},
        "data_start": datetime.fromtimestamp(bars[0].t, timezone.utc).isoformat(),
        "data_end_exclusive": datetime.fromtimestamp(bars[-1].t + 3600, timezone.utc).isoformat(),
        "limitations": [
            "Already-seen BTC market history: chronological periods are exploratory, not a blind experiment.",
            "Each named period restarts with0.003BTC; continuous_full is one self-financing account.",
            "All cash is USDT. BTC-denominated cash value falls when BTCUSDT rises and rises when BTCUSDT falls.",
            "Final heldBTC excludes unconvertible USDT dust; total netBTC includes its close-price valuation.",
            "No historical orderbook, fee-tier history, exchange outages, or changing instrument filters are reconstructed.",
            "Signals use actual closed exchange1d candles, not daily bars fabricated from missing hourly records.",
            "Missing/partial hourly API records are excluded as documented; no interpolated fills or gap-cause assumption.",
            "No live account reads, API keys, order routing, transfers, or alteration of existing paper/live strategies.",
        ],
        "commission_reference": "https://developers.binance.com/en/docs/products/spot/faqs/commission_faq",
    }
    protocol_path = output / "protocol.json"
    if protocol_path.exists():
        previous = json.loads(protocol_path.read_text(encoding="utf-8"))
        strip = lambda value: {k: v for k, v in clean(value).items() if k != "created_at"}
        if strip(previous) != strip(protocol):
            raise ValueError("Protocol changed: use a new output directory")
        protocol = previous
    else:
        save(protocol_path, protocol)
    # The comparison is frozen above before computing any strategy returns.
    rows = {}
    for name, family, band in CASES:
        targets = target_schedule(days, family)
        if stamp("2021-04-01") not in targets:
            raise ValueError("Insufficient spot warmup at the declared study start")
        rows[name] = {}
        for period, (begin, finish) in PERIODS.items():
            lo, hi = stamp(begin), stamp(finish) if finish else bars[-1].t + 3600
            selected = [bar for bar in bars if lo <= bar.t < hi]
            selected_targets = {t: weight for t, weight in targets.items() if lo <= t < hi}
            rows[name][period] = {}
            for cost, factor in (("base", 1), ("stress_2x", 2)):
                result = simulate_spot(selected, selected_targets, filters,
                                       SpotConfig(fee_rate=.001 * factor, slippage_bps=3 * factor,
                                                  no_trade_band=band))
                metrics = growth_metrics(result)
                rows[name][period][cost] = {**result["summary"], "growth": metrics,
                                           "pre_terminal_conversion": result["pre_terminal_conversion"]}
                save(output / f"{name}_{period}_{cost}.json", result)
        print(name, "complete", flush=True)
        save(output / "partial_results.json", rows)
    report = {"protocol": protocol, "results": rows,
              "selection": {"selected": None, "recent_used_for_selection": False, "live_eligible": False}}
    save(output / "report.json", report)
    lines = ["# 0.003 BTC 현물 BTC/USDT 고정 비교", "",
             "실제 BTCUSDT 현물 데이터. 모든 계좌는 BTC만 보유한 상태에서 시작하며 외부 자금 추가가 없다.",
             "순평가 BTC에는 USDT 잔액의 BTC 환산액이 포함된다. 최종 보유 BTC는 실제 최소수량·비용을 적용한 최종환전 이후 수량이다.", ""]
    for period in PERIODS:
        lines += [f"## {period}", "", "| 규칙 | 순 BTC 수익 | 비용2배 | 최종 보유 BTC | USDT 잔여 | BTC MDD | 체결 수 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for name, _, _ in CASES:
            a, s = rows[name][period]["base"], rows[name][period]["stress_2x"]
            lines.append(f"| {name} | {a['return_pct']:+.2f}% | {s['return_pct']:+.2f}% | "
                         f"{a['final_held_btc']:.8f} | {a['quote_dust_usdt']:.6f} | {a['max_drawdown_pct']:.2f}% | {a['trade_count']} |")
        lines.append("")
    lines += ["관찰 결과로 최근 승자를 골라 실거래에 적용하지 않는다. 현금 보유 중 BTC 가격 상승은 BTC 수량 기준 손실이 될 수 있다.",
              "매수 수수료는 받은 BTC에서, 매도 수수료는 받은 USDT에서 차감한다. 현재 확인한 수수료·최소 주문 규칙을 과거에도 일정하게 적용한 연구다."]
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=Path, default=ROOT / "btc_lab/state/small_capital_market/BTCUSDT_1h_spot.csv")
    parser.add_argument("--exchange-info", type=Path, default=ROOT / "btc_lab/state/market_fit_20260924/exchange_info_BTCUSDT_spot.json")
    parser.add_argument("--daily-data", type=Path, help="Actual BTCUSDT1d CSV, default is the hourly CSV sibling")
    parser.add_argument("--quality-manifest", type=Path, help="Spot downloader audit manifest")
    parser.add_argument("--output", type=Path, default=ROOT / "btc_lab/state/small_spot_20260924")
    args = parser.parse_args()
    run_study(args.candles, args.exchange_info, args.output,
              daily_data=args.daily_data, quality_manifest=args.quality_manifest)
