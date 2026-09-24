"""Isolated small-capital sizing study; copied inverse research accounting.

Targets are signed USD notionals divided by BTC equity valued at the mark price.
Their keys are *execution* hour openings; callers must generate them using only
previously closed bars. This module deliberately has no exchange or bot imports.
It models one common BTC collateral wallet, not exchange isolated subaccounts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping, Sequence

from . import engine as frozen_engine


@dataclass(frozen=True)
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    mark_o: float
    mark_h: float
    mark_l: float
    mark_c: float


@dataclass(frozen=True)
class Funding:
    t: float
    rate: float
    mark: float | None = None


@dataclass(frozen=True)
class Spec:
    contract_size: float = 100.0
    qty_step: float = 1.0
    min_qty: float = 1.0
    tick: float = 0.1
    maint_margin_rate: float = 0.004


@dataclass(frozen=True)
class Config:
    initial_btc: float = 0.007
    fee: float = 0.0005
    slip_bps: float = 3.0
    stop_slip_bps: float = 10.0
    max_exposure: float = 1.0
    leverage: float = 3.0
    stop_pct: float = 0.10
    liquidation_fee: float = 0.005
    intrabar_funding_policy: str = "before_stop"
    sizing_policy: str = "original_floor"


def _finite(value: float) -> bool:
    return math.isfinite(value)


def _validate(bars: Sequence[Bar], funding: Sequence[Funding],
              targets: Mapping[int, float], spec: Spec, cfg: Config) -> None:
    if not bars:
        raise ValueError("At least one complete hourly bar is required")
    for name, value in asdict(spec).items():
        if not _finite(value) or value <= 0:
            raise ValueError(f"Invalid contract parameter: {name}")
    if spec.maint_margin_rate >= 1:
        raise ValueError("Maintenance margin rate must be below one")
    for name, value in asdict(cfg).items():
        if name in {"intrabar_funding_policy", "sizing_policy"}:
            continue
        if not _finite(value) or value < 0:
            raise ValueError(f"Invalid configuration parameter: {name}")
    if cfg.intrabar_funding_policy not in {"before_stop", "adverse"}:
        raise ValueError("Unknown intra-hour funding policy")
    if cfg.sizing_policy not in {"original_floor", "nearest", "floor_hysteresis"}:
        raise ValueError("Unknown frozen sizing policy")
    if min(cfg.initial_btc, cfg.max_exposure, cfg.leverage) <= 0:
        raise ValueError("Initial BTC, leverage and exposure limit must be positive")
    if not 0 < cfg.stop_pct < 1:
        raise ValueError("Stop fraction must be between zero and one")
    if max(cfg.slip_bps, cfg.stop_slip_bps) >= 10_000:
        raise ValueError("Slippage must be below 100 percent")
    if cfg.fee + spec.maint_margin_rate >= 1 or cfg.liquidation_fee >= 1:
        raise ValueError("Invalid fee or maintenance margin rate")
    opens = set()
    previous = None
    for bar in bars:
        if not _finite(bar.t) or bar.t != int(bar.t):
            raise ValueError("Bar opening timestamps must be integer seconds")
        if previous is not None and bar.t - previous != 3600:
            raise ValueError("Bars must be contiguous, increasing hourly bars")
        previous = bar.t
        opens.add(bar.t)
        for prefix in ("", "mark_"):
            o, h, l, c = (getattr(bar, prefix + x) for x in "ohlc")
            if not all(_finite(x) and x > 0 for x in (o, h, l, c)):
                raise ValueError("OHLC prices must be finite and positive")
            if not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError("Inconsistent OHLC range")
    for timestamp, target in targets.items():
        if timestamp not in opens:
            raise ValueError("Each target timestamp must be an execution bar opening")
        if not _finite(target):
            raise ValueError("Target exposures must be finite")
    seen = set()
    for event in funding:
        if not _finite(event.t) or not _finite(event.rate):
            raise ValueError("Funding timestamps and rates must be finite")
        if event.t in seen:
            raise ValueError("Duplicate funding timestamp")
        seen.add(event.t)
        if event.mark is not None and (not _finite(event.mark) or event.mark <= 0):
            raise ValueError("Funding mark must be finite and positive")


def simulate_policy(bars: Sequence[Bar], funding: Sequence[Funding],
        targets: Mapping[int, float], spec: Spec | None = None,
        cfg: Config | None = None) -> dict:
    """Run a deterministic inverse-contract simulation, returning plain data.

    BTC net PnL is ``direction * contracts * USD_size * (1/entry - 1/exit)``.
    Entries/additions use harmonic average prices. Stops can tighten on additions
    but never loosen. Open-time funding belongs to the position already held;
    intra-hour funding normally precedes intra-hour stops. The optional adverse
    policy withholds credits on a bar that could close protectively with all
    funding debits applied and credits omitted. This is an accounting stress,
    not a reconstruction of unknown intrabar order. Final positions close at the
    last contract close with costs; the caller must supply completed bars only.
    """
    spec, cfg = spec or Spec(), cfg or Config()
    _validate(bars, funding, targets, spec, cfg)
    if cfg.sizing_policy == "original_floor":
        original = {key: value for key, value in asdict(cfg).items() if key != "sizing_policy"}
        return frozen_engine.run(bars, funding, targets, spec, frozen_engine.Config(**original))
    cash = cfg.initial_btc
    position = entry = stop = 0.0
    fees = funding_total = realized = liquidation_fees = 0.0
    trades: list[dict] = []
    events: list[dict] = []
    curve: list[dict] = []
    active: dict | None = None
    audit = {"target_clamps": 0, "margin_caps": 0, "below_minimum": 0,
             "missing_funding_marks": 0, "intrabar_funding": 0,
             "stops": 0, "liquidations": 0, "gap_exits": 0,
             "targets_blocked_after_gap_exit": 0, "max_added_exposure": 0.0,
             "withheld_funding_events": 0, "withheld_funding_credit_btc": 0.0,
             "hysteresis_kept": 0, "budget_forced_reductions": 0,
             "maximum_decision_exposure": 0.0}
    cs = spec.contract_size

    def quantize(qty: float) -> float:
        return math.floor(max(qty, 0.0) / spec.qty_step + 1e-12) * spec.qty_step

    def equity(price: float) -> float:
        return cash + (position * cs * (1 / entry - 1 / price) if position else 0)

    def fill_price(raw: float, side: int, bps: float) -> float:
        slipped = raw * (1 + side * bps / 10_000)
        ticks = slipped / spec.tick
        rounded = (math.ceil(ticks - 1e-10) if side > 0 else math.floor(ticks + 1e-10)) * spec.tick
        if rounded <= 0:
            raise ValueError("Price becomes nonpositive after execution costs")
        return rounded

    def execute(delta: float, raw: float, timestamp: float, reason: str,
                *, forced: bool = False, liquidation: bool = False) -> None:
        nonlocal cash, position, entry, stop, fees, realized, active, liquidation_fees
        if not delta:
            return
        side = 1 if delta > 0 else -1
        px = fill_price(raw, side, cfg.stop_slip_bps if forced else cfg.slip_bps)
        quantity = abs(delta)
        fee = quantity * cs / px * cfg.fee
        insurance = quantity * cs / px * cfg.liquidation_fee if liquidation else 0.0
        pnl = 0.0
        old_position = position
        is_add = not position or delta * position > 0
        if is_add:
            if active is None:
                active = {"direction": side, "opened_at": timestamp,
                          "equity_at_entry": equity(px), "initial_qty": quantity,
                          "max_qty": quantity, "realized_btc": 0.0,
                          "fee_btc": 0.0, "funding_btc": 0.0,
                          "liquidation_fee_btc": 0.0}
            entry = ((abs(position) + quantity) /
                     ((abs(position) / entry if position else 0) + quantity / px))
            position += delta
            new_stop = entry * (1 - cfg.stop_pct if position > 0 else 1 + cfg.stop_pct)
            stop = (max(stop, new_stop) if position > 0 else min(stop, new_stop)) if stop else new_stop
            active["max_qty"] = max(active["max_qty"], abs(position))
        else:
            if quantity > abs(position) + 1e-9:
                raise AssertionError("A reversal must close before opening")
            pnl = (1 if position > 0 else -1) * quantity * cs * (1 / entry - 1 / px)
            position += delta
            if abs(position) < spec.qty_step * 1e-8:
                position = 0.0
        cash += pnl - fee - insurance
        fees += fee
        realized += pnl
        liquidation_fees += insurance
        assert active is not None
        active["fee_btc"] += fee
        active["realized_btc"] += pnl
        active["liquidation_fee_btc"] += insurance
        events.append({"type": "fill", "t": timestamp, "reason": reason,
                       "qty": delta, "price": px, "fee_btc": fee,
                       "realized_btc": pnl, "liquidation_fee_btc": insurance,
                       "position_before": old_position, "position_after": position,
                       "cash_btc": cash})
        if not position:
            active.update(closed_at=timestamp, reason=reason,
                          net_btc=active["realized_btc"] + active["funding_btc"] -
                          active["fee_btc"] - active["liquidation_fee_btc"])
            trades.append(active)
            active = None
            entry = stop = 0.0

    def accrue(event: Funding, bar: Bar, *, omit_positive: bool = False) -> None:
        nonlocal cash, funding_total
        if not position:
            return
        price = event.mark if event.mark is not None else bar.mark_o
        audit["missing_funding_marks"] += int(event.mark is None)
        amount = -position * cs / price * event.rate
        nominal_amount = amount
        if omit_positive and amount > 0:
            audit["withheld_funding_events"] += 1
            audit["withheld_funding_credit_btc"] += amount
            amount = 0.0
        cash += amount
        funding_total += amount
        assert active is not None
        active["funding_btc"] += amount
        events.append({"type": "funding", "t": event.t, "rate": event.rate,
                       "mark_price": price, "estimated_mark": event.mark is None,
                       "position": position, "funding_btc": amount, "cash_btc": cash,
                       "nominal_funding_btc": nominal_amount,
                       "credit_withheld": nominal_amount > amount})

    def adverse_credit_exclusion(bar: Bar, bar_funding: Sequence[Funding]) -> bool:
        if cfg.intrabar_funding_policy != "adverse" or not position or not bar_funding:
            return False
        worst = bar.mark_l if position > 0 else bar.mark_h
        stop_hit = worst <= stop if position > 0 else worst >= stop
        debits = sum(min(0.0, -position * cs /
                         (event.mark if event.mark is not None else bar.mark_o) * event.rate)
                     for event in bar_funding)
        maintenance = abs(position) * cs / worst * (spec.maint_margin_rate + cfg.fee)
        return stop_hit or equity(worst) + debits <= maintenance

    def liquidation_price() -> float | None:
        if not position:
            return None
        qv = abs(position) * cs
        maintenance = spec.maint_margin_rate + cfg.fee
        denominator = cash + qv / entry if position > 0 else qv / entry - cash
        if denominator <= 0:
            return None
        return qv * (1 + maintenance if position > 0 else 1 - maintenance) / denominator

    def below_maintenance(price: float) -> bool:
        return bool(position and equity(price) <= abs(position) * cs / price *
                    (spec.maint_margin_rate + cfg.fee))

    def protective_exit(bar: Bar, gap_only: bool) -> bool:
        if not position:
            return False
        direction = 1 if position > 0 else -1
        gap_liquidation = below_maintenance(bar.mark_o)
        gap_stop = bar.mark_o <= stop if direction > 0 else bar.mark_o >= stop
        reason = None
        raw = bar.o
        gap = gap_liquidation or gap_stop
        if gap:
            reason = "liquidation" if gap_liquidation else "stop"
        elif not gap_only:
            liq = liquidation_price()
            worst = bar.mark_l if direction > 0 else bar.mark_h
            stop_hit = worst <= stop if direction > 0 else worst >= stop
            liq_hit = liq is not None and (worst <= liq if direction > 0 else worst >= liq)
            if stop_hit or liq_hit:
                # On a continuous path the nearest crossed level occurs first.
                if liq_hit and (not stop_hit or (liq > stop if direction > 0 else liq < stop)):
                    level, reason = liq, "liquidation"
                else:
                    level, reason = stop, "stop"
                # Opening basis is only a proxy for unobserved trigger-time basis.
                raw = min(bar.h, max(bar.l, level + bar.o - bar.mark_o))
        if reason is None:
            return False
        audit["gap_exits"] += int(gap)
        audit["liquidations" if reason == "liquidation" else "stops"] += 1
        execute(-position, raw, bar.t if gap else bar.t + 3600,
                reason, forced=True, liquidation=reason == "liquidation")
        return True

    def rebalance(target: float, bar: Bar) -> None:
        bounded = min(cfg.max_exposure, max(-cfg.max_exposure, target))
        audit["target_clamps"] += int(bounded != target)
        if equity(bar.mark_o) <= 0:
            return
        desired_sign = 1 if bounded > 0 else -1 if bounded < 0 else 0
        if position and position * desired_sign <= 0:
            execute(-position, bar.o, bar.t, "flat" if not desired_sign else "reverse")
        if not desired_sign:
            return
        raw_requested = equity(bar.mark_o) * bar.mark_o * abs(bounded) / cs
        current = abs(position)
        if cfg.sizing_policy == "nearest":
            requested = math.floor(raw_requested / spec.qty_step + .5) * spec.qty_step
        elif current and abs(raw_requested - current) <= .5 * spec.qty_step:
            requested = current
            audit["hysteresis_kept"] += 1
        else:
            requested = quantize(raw_requested)
        if requested < spec.min_qty:
            audit["below_minimum"] += 1
            requested = 0.0
        # The budget includes entry/exit costs and opening mark/contract basis.
        # It is a decision-time constraint, not a guarantee during later gaps.
        hard_limit = min(cfg.leverage, cfg.max_exposure,
                         (1 - 1e-10) / (spec.maint_margin_rate + cfg.fee))
        available = max(0.0, equity(bar.mark_o))
        if requested > current:
            px = fill_price(bar.o, desired_sign, cfg.slip_bps)
            # Each added contract changes marked equity by both its entry fee
            # and immediate inverse PnL at the opening mark (including basis).
            cost_per_added = cs * (cfg.fee / px - desired_sign * (1 / px - 1 / bar.mark_o))
            numerator = available + current * cost_per_added

            def cap(limit: float) -> float:
                denominator = cs / (bar.mark_o * limit) + cost_per_added
                return max(0.0, numerator / denominator) if denominator > 0 else requested

            allowed = quantize(min(requested, cap(hard_limit)))
            audit["margin_caps"] += int(allowed < requested)
            requested = allowed
        # Retention and reductions must also fit after paying the exit fee and
        # adverse slippage. Hysteresis cannot retain an over-budget position.
        if current and requested <= current:
            px = fill_price(bar.o, -desired_sign, cfg.slip_bps)
            cost_per_closed = cs * (cfg.fee / px - desired_sign * (1 / bar.mark_o - 1 / px))
            numerator = available - current * cost_per_closed
            denominator = cs / (bar.mark_o * hard_limit) - cost_per_closed
            reduction_cap = max(0.0, numerator / denominator) if denominator > 0 else requested
            allowed = quantize(min(requested, reduction_cap))
            audit["budget_forced_reductions"] += int(allowed < requested)
            requested = allowed
        if requested < spec.min_qty:
            requested = 0.0
        delta = desired_sign * requested - position
        if delta and (abs(delta) >= spec.min_qty or delta * position < 0):
            adding = not position or delta * position > 0
            execute(delta, bar.o, bar.t, "rebalance")
            if adding and equity(bar.mark_o) > 0:
                audit["max_added_exposure"] = max(audit["max_added_exposure"],
                    abs(position) * cs / bar.mark_o / equity(bar.mark_o))
        if position:
            after_equity = equity(bar.mark_o)
            actual_exposure = abs(position) * cs / bar.mark_o / after_equity if after_equity > 0 else math.inf
            if actual_exposure > hard_limit + 1e-9:
                raise AssertionError("Sizing decision exceeded its after-cost hard budget")
            audit["maximum_decision_exposure"] = max(audit["maximum_decision_exposure"], actual_exposure)

    def record(timestamp: int, mark: float) -> None:
        value = equity(mark)
        curve.append({"t": timestamp, "equity_btc": value, "cash_btc": cash,
                      "position": position, "mark_price": mark,
                      "exposure": position * cs / mark / value if value > 0 else 0.0})

    record(bars[0].t, bars[0].mark_o)
    funding_events = sorted((event for event in funding
                             if bars[0].t <= event.t < bars[-1].t + 3600), key=lambda x: x.t)
    funding_index = 0
    for bar in bars:
        while funding_index < len(funding_events) and funding_events[funding_index].t <= bar.t:
            accrue(funding_events[funding_index], bar)
            funding_index += 1
        gap_exit = protective_exit(bar, gap_only=True)
        if bar.t in targets:
            if gap_exit:
                audit["targets_blocked_after_gap_exit"] += 1
            else:
                rebalance(targets[bar.t], bar)
        bar_funding = []
        while funding_index < len(funding_events) and funding_events[funding_index].t < bar.t + 3600:
            audit["intrabar_funding"] += 1
            bar_funding.append(funding_events[funding_index])
            funding_index += 1
        # This preview occurs after targets execute. Future intrabar funding and
        # extremes never alter this bar's target or its opening contract count.
        omit_credits = adverse_credit_exclusion(bar, bar_funding)
        for event in bar_funding:
            accrue(event, bar, omit_positive=omit_credits)
        protective_exit(bar, gap_only=False)
        record(bar.t + 3600, bar.mark_c)
    if position:
        execute(-position, bars[-1].c, bars[-1].t + 3600, "end_of_data")
        curve.pop()
        record(bars[-1].t + 3600, bars[-1].mark_c)
    peak = cfg.initial_btc
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point["equity_btc"])
        max_drawdown = max(max_drawdown, 1 - point["equity_btc"] / peak)
    ledger_expected = cfg.initial_btc + realized + funding_total - fees - liquidation_fees
    ledger_error = cash - ledger_expected
    if not math.isclose(cash, ledger_expected, rel_tol=1e-10, abs_tol=1e-12):
        raise AssertionError("BTC wallet does not reconcile with the ledger")
    return {
        "summary": {"initial_btc": cfg.initial_btc, "final_btc": cash,
                    "net_btc": cash - cfg.initial_btc,
                    "return_pct": (cash / cfg.initial_btc - 1) * 100,
                    "max_drawdown_pct": max_drawdown * 100,
                    "fee_btc": fees, "funding_btc": funding_total,
                    "realized_btc": realized, "liquidation_fee_btc": liquidation_fees,
                    "trade_count": len(trades), "fill_count": sum(x["type"] == "fill" for x in events),
                    "liquidations": audit["liquidations"], "bankrupt": cash <= 0,
                    "ledger_error_btc": ledger_error},
        "equity_curve": curve, "trades": trades, "events": events, "audit": audit,
        "config": asdict(cfg), "spec": asdict(spec),
        "assumptions": [
            "One shared BTC collateral wallet; not an exchange isolated-margin account.",
            "Targets execute at hour openings; caller must exclude incomplete and future bars.",
            "All executions pay taker fees, adverse slippage and adverse tick rounding.",
            "Stops use marks; trigger-time contract basis approximates the opening basis.",
            "Open gaps execute at contract open; otherwise the nearest protective level crosses first.",
            "Funding exactly at open precedes new entries; intra-hour funding is processed after target executions.",
            ("Intra-hour funding precedes stops; positive credits can overstate receipts when a stop actually came first."
             if cfg.intrabar_funding_policy == "before_stop" else
             "Adverse accounting: withhold intra-hour funding credits if a protective exit is possible with debits charged and credits omitted; retain all debits."),
            "Intrabar funding/stop ordering is unknown; adverse policy is a stress assumption, not a guaranteed worst-case path.",
            "Missing funding marks use the corresponding hourly opening mark.",
            "Maintenance rate is constant; liquidation fee is a configurable research assumption.",
            "Exposure and initial margin caps apply when adding; they do not constantly rebalance losses.",
            "No liquidation depth, ADL, exchange outages, insurance guarantee or conversion costs modeled.",
            "Maximum drawdown uses hourly marked equity and includes the initial balance.",
            "Experimental sizing policy; nearest may exceed desired target but all new-policy decisions obey after-cost hard budgets.",
            "Hysteresis uses one fixed half-contract band; zero targets, reversals and protective exits are immediate.",
        ],
    }


def decision_diagnostics(result, bars, targets, spec):
    """Reconstruct marked exposure after opening events, for all three policies."""
    cash = result["summary"]["initial_btc"]
    position = entry = 0.0
    index = 0
    events = result["events"]
    by_time = {bar.t: bar for bar in bars}
    records = []
    for timestamp, target in sorted(targets.items()):
        while index < len(events) and events[index]["t"] <= timestamp:
            event = events[index]
            if event["type"] == "fill":
                delta, price = event["qty"], event["price"]
                if not position or delta * position > 0:
                    entry = ((abs(position) + abs(delta)) /
                             ((abs(position) / entry if position else 0) + abs(delta) / price))
                position = event["position_after"]
                if not position:
                    entry = 0.0
            cash = event["cash_btc"]
            index += 1
        mark = by_time[timestamp].mark_o
        value = cash + (position * spec.contract_size * (1 / entry - 1 / mark) if position else 0)
        exposure = position * spec.contract_size / mark / value if value > 0 else 0.0
        records.append({"t": timestamp, "target": target, "actual_exposure": exposure,
                        "tracking_error": abs(exposure - target), "position": position})
    errors = sorted(record["tracking_error"] for record in records)
    return {"decision_count": len(records),
            "mean_absolute_tracking_error_pct_points": sum(errors) / len(errors) * 100 if errors else None,
            "p95_absolute_tracking_error_pct_points": errors[min(len(errors)-1, math.ceil(.95*len(errors))-1)] * 100 if errors else None,
            "maximum_decision_absolute_exposure": max((abs(row["actual_exposure"]) for row in records), default=0),
            "maximum_hour_close_absolute_exposure": max(abs(row["exposure"]) for row in result["equity_curve"]),
            "maximum_absolute_contracts": max((abs(event[key]) for event in events if event["type"] == "fill"
                                               for key in ("position_before", "position_after")), default=0),
            "sizing_records": records}


def run_study(cache, output):
    """Run the twelve predetermined comparisons without updating paper config."""
    from dataclasses import replace
    from datetime import datetime, timezone
    import hashlib
    import json
    from pathlib import Path
    from .growth_metrics import growth_metrics
    from .research import (clean, completed_days, enrich, period_input, read_market,
                           save, target_schedule, ts)

    cache, output = Path(cache), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    bars, funding, raw_spec, quality = read_market(cache)
    spec = replace(raw_spec, maint_margin_rate=.004)
    policies = ("original_floor", "nearest", "floor_hysteresis")
    periods = {"continuous_from_20210401": "2021-04-01", "recent_reset_20250101": "2025-01-01"}
    source_files = [Path(__file__), Path(__file__).with_name("engine.py"),
                    Path(__file__).with_name("research.py"), Path(__file__).with_name("growth_metrics.py")]
    protocol = {"created_utc": datetime.now(timezone.utc).isoformat(),
        "signal": "momentum_20_60_120", "annual_volatility_target": .60, "exposure_cap": 2,
        "stop_pct": .20, "initial_btc": .003, "fee": .0005, "maintenance_rate": .004,
        "policies": policies, "hysteresis_band_contract_steps": .5,
        "nearest_ties": "half up, subject to hard budgets; no forced minimum order",
        "hard_budget_scope": "daily decision immediately after costs; not later price movement",
        "periods": periods, "cost_factors": {"base": 1, "stress_2x": 2}, "runs": 12,
        "spec": asdict(spec), "data_quality": quality,
        "frozen_paper_candidate": "momentum60_stop20", "paper_policy_changed": False,
        "orders_enabled": False, "historical_winner_selected": False,
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files},
        "data_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(cache.iterdir())
                        if p.name.startswith(("BTCUSD_PERP", "exchange_info_BTCUSD_PERP"))},
        "limitations": [
            "Reused historical data; experimental policy comparison, no independent out-of-sample proof.",
            "Nearest may exceed desired target. The daily after-cost hard cap is still enforced.",
            "Hysteresis can retain more than the desired target within a fixed half-contract band.",
            "The original control delegates to the unchanged original engine and preserves its exact behavior.",
            "New policies enforce budget reductions at decisions; price movement can breach caps between decisions.",
            "Current first maintenance bracket is a constant historical approximation, not historical verification.",
            "Shared collateral, intrahour paths, liquidity and liquidation costs remain approximate.",
            "No policy is promoted to the existing PC paper runner from these outcomes."]}
    protocol_path = output / "protocol.json"
    if protocol_path.exists():
        previous = json.loads(protocol_path.read_text(encoding="utf-8"))
        strip = lambda item: {key: value for key, value in clean(item).items() if key != "created_utc"}
        if strip(previous) != strip(protocol):
            raise ValueError("Frozen sizing protocol changed; use a different output directory")
        protocol = previous
    else:
        save(protocol_path, protocol)
    # The protocol above is written before any outcome is computed.
    targets = target_schedule(completed_days(bars), "momentum_20_60_120", .60, 2)
    results = {}
    for policy in policies:
        results[policy] = {}
        for period, date in periods.items():
            b, f, t = period_input(bars, funding, targets, ts(date), bars[-1].t + 3600)
            if not b or b[0].t != ts(date):
                raise ValueError("Fixed study start is missing")
            results[policy][period] = {}
            for cost, factor in (("base", 1), ("stress_2x", 2)):
                cfg = Config(initial_btc=.003, fee=.0005*factor, slip_bps=3*factor,
                             stop_slip_bps=10*factor, max_exposure=2, leverage=3,
                             stop_pct=.20, intrabar_funding_policy="adverse", sizing_policy=policy)
                result = simulate_policy(b, f, t, spec, cfg)
                sizing = decision_diagnostics(result, b, t, spec)
                stats = enrich(result)
                stats["growth"] = growth_metrics(result)
                stats["sizing"] = {key: value for key, value in sizing.items() if key != "sizing_records"}
                stats["policy_audit"] = result["audit"]
                results[policy][period][cost] = stats
                save(output / f"{policy}_{period}_{cost}.json", result)
                save(output / f"{policy}_{period}_{cost}_sizing.json", sizing)
                print(json.dumps({"policy": policy, "period": period, "cost": cost,
                    "return_pct": stats["return_pct"], "max_drawdown_pct": stats["max_drawdown_pct"],
                    "fee_btc": stats["fee_btc"], "trade_count": stats["trade_count"],
                    "tracking_error_pct_points": stats["sizing"]["mean_absolute_tracking_error_pct_points"]}), flush=True)
    report = {"protocol": protocol, "results": results, "paper_policy_changed": False,
              "historical_winner_selected": False, "orders_enabled": False}
    save(output / "report.json", report)
    lines = ["# 0.003 BTC 정수 계약 수량 정책 비교", "",
             "동일 momentum60/20%손절 신호. 정책 3개를 결과 확인 전에 고정. PC 종이관측 설정 변경 없음.",
             "목표와 실제 노출의 차이는 일별 수량 결정 직후 mark 평가 기준. 결정 이후 가격변동은 노출을 높일 수 있다.", "",
             "| 정책 | 기간 | 비용 | BTC 수익 | DD | 수수료 BTC | 거래수 | 평균 추종오차(%p) | 최대 결정노출 |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for policy, periods_result in results.items():
        for period, costs in periods_result.items():
            for cost, stats in costs.items():
                sizing = stats["sizing"]
                lines.append(f"| {policy} | {period} | {cost} | {stats['return_pct']:+.6f}% | "
                             f"{stats['max_drawdown_pct']:.6f}% | {stats['fee_btc']:.9f} | {stats['trade_count']} | "
                             f"{sizing['mean_absolute_tracking_error_pct_points']:.3f} | {sizing['maximum_decision_absolute_exposure']:.6f} |")
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Fixed COIN-M small-capital sizing research; no orders")
    parser.add_argument("--cache", type=Path, default=root / "binance_coinm_v1/state/cache")
    parser.add_argument("--output", type=Path, default=root / "btc_lab/state/small_coinm_20260924")
    args = parser.parse_args()
    run_study(args.cache, args.output)
