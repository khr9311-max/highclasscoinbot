"""Independent, public-data-only inverse futures research accounting.

Targets are signed USD notionals divided by BTC equity valued at the mark price.
Their keys are *execution* hour openings; callers must generate them using only
previously closed bars. This module deliberately has no exchange or bot imports.
It models one common BTC collateral wallet, not exchange isolated subaccounts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping, Sequence


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
        if name == "intrabar_funding_policy":
            continue
        if not _finite(value) or value < 0:
            raise ValueError(f"Invalid configuration parameter: {name}")
    if cfg.intrabar_funding_policy not in {"before_stop", "adverse"}:
        raise ValueError("Unknown intra-hour funding policy")
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


def run(bars: Sequence[Bar], funding: Sequence[Funding],
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
             "withheld_funding_events": 0, "withheld_funding_credit_btc": 0.0}
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
        requested = quantize(equity(bar.mark_o) * bar.mark_o * abs(bounded) / cs)
        if requested < spec.min_qty:
            audit["below_minimum"] += 1
            requested = 0.0
        current = abs(position)
        if requested > current:
            px = fill_price(bar.o, desired_sign, cfg.slip_bps)
            available = max(0.0, equity(bar.mark_o))
            # Each added contract changes marked equity by both its entry fee
            # and immediate inverse PnL at the opening mark (including basis).
            cost_per_added = cs * (cfg.fee / px - desired_sign * (1 / px - 1 / bar.mark_o))
            numerator = available + current * cost_per_added

            def cap(limit: float) -> float:
                denominator = cs / (bar.mark_o * limit) + cost_per_added
                return max(0.0, numerator / denominator) if denominator > 0 else requested

            margin_cap = cap(cfg.leverage)
            exposure_cap = cap(cfg.max_exposure)
            allowed = quantize(min(requested, margin_cap, exposure_cap))
            audit["margin_caps"] += int(allowed < requested)
            requested = max(current, allowed)
        delta = desired_sign * requested - position
        if delta and (abs(delta) >= spec.min_qty or delta * position < 0):
            adding = not position or delta * position > 0
            execute(delta, bar.o, bar.t, "rebalance")
            if adding and equity(bar.mark_o) > 0:
                audit["max_added_exposure"] = max(audit["max_added_exposure"],
                    abs(position) * cs / bar.mark_o / equity(bar.mark_o))

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
        ],
    }
