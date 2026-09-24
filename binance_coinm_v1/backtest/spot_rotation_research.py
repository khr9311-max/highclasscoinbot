"""Offline BTC-quoted spot momentum study. Public GET only; no trading credentials.

Two fixed lookbacks (20/60 days), weekly selection, BTC cash when none is positive.
At most 10% initial portfolio allocation; 5% stop, sizing caps planned loss at 0.5%.
This is a separate exploratory strategy, not a supported production trading mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import numpy as np

from ..strategy.price_action import Bars
from .metrics import max_drawdown
from .research import PACKAGE, iso, timestamp, write_json

SYMBOLS = ("ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC")
LOOKBACKS = (20, 60)
PERIOD = 14400
END = timestamp("2026-09-24") + 8 * 3600
SPLITS = {"2024": (timestamp("2024-01-01"), timestamp("2025-01-01")),
          "recent": (timestamp("2025-01-01"), END),
          "full": (timestamp("2024-01-01"), END)}


def public_get(path, params):
    url = "https://api.binance.com" + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def load_public_data(output):
    cache = output / "cache"
    cache.mkdir(exist_ok=True)
    info_path = cache / "exchange_info.json"
    if not info_path.exists():
        write_json(info_path, public_get("/api/v3/exchangeInfo",
                                        {"symbols": json.dumps(SYMBOLS, separators=(",", ":"))}))
    info = json.loads(info_path.read_text(encoding="utf-8"))
    specs = {x["symbol"]: x for x in info["symbols"]}
    bars = {}
    for symbol in SYMBOLS:
        path = cache / (symbol + "_4h.json")
        if not path.exists():
            cursor = int(timestamp("2023-10-01") * 1000)
            rows = []
            while cursor < END * 1000:
                chunk = public_get("/api/v3/klines", {"symbol": symbol, "interval": "4h",
                                   "startTime": cursor, "endTime": int(END * 1000) - 1, "limit": 1000})
                if not chunk:
                    raise ValueError(f"Missing public history for {symbol}")
                rows.extend(x for x in chunk if int(x[6]) < END * 1000)
                next_cursor = int(chunk[-1][0]) + PERIOD * 1000
                if next_cursor <= cursor:
                    raise ValueError("History pagination did not advance")
                cursor = next_cursor
                time.sleep(.1)
            write_json(path, rows)
        raw = json.loads(path.read_text(encoding="utf-8"))
        b = Bars.from_rows([(int(x[0]) / 1000, *map(float, x[1:6])) for x in raw], PERIOD)
        if not len(b) or np.any(np.diff(b.t) != PERIOD):
            raise ValueError(f"Noncontiguous spot data: {symbol}")
        values = np.column_stack([b.o, b.h, b.l, b.c])
        if not np.isfinite(values).all() or (values <= 0).any() or \
                (b.h < np.maximum(b.o, b.c)).any() or (b.l > np.minimum(b.o, b.c)).any():
            raise ValueError(f"Invalid spot OHLC: {symbol}")
        if bars and not np.array_equal(b.t, next(iter(bars.values())).t):
            raise ValueError("Spot market timestamps differ; no forward filling is allowed")
        bars[symbol] = b
        print(f"Spot {symbol}: {len(b)} completed 4h bars", flush=True)
    return bars, specs


def weekly_choices(markets, days):
    """At Monday 00:00 open, use only the just-completed Sunday daily close."""
    if days not in LOOKBACKS:
        raise ValueError("Only prespecified 20/60 day rules")
    times = next(iter(markets.values())).t
    choices = {}
    lag = days * 6
    for i in range(lag + 1, len(times)):
        dt = datetime.fromtimestamp(float(times[i]), timezone.utc)
        if dt.weekday() != 0 or dt.hour != 0:
            continue
        ranking = [(float(b.c[i - 1] / b.c[i - 1 - lag] - 1), symbol)
                   for symbol, b in markets.items()]
        momentum, symbol = max(ranking)
        choices[i] = symbol if momentum > 0 else None
    return choices


def quantity_for_budget(raw, equity, spec, fee, slip, stop_slip):
    filters = {f["filterType"]: f for f in spec["filters"]}
    entry = raw * (1 + slip)
    stop = raw * .95
    tick = Decimal(filters.get("PRICE_FILTER", {}).get("tickSize", "0"))
    if tick > 0:
        stop = float((Decimal(str(stop)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick)
    if stop <= 0:
        return 0.0, entry, stop
    stop_fill = stop * (1 - stop_slip)
    loss_per_unit = entry * (1 + fee) - stop_fill * (1 - fee)
    desired = min(equity * .005 / loss_per_unit, equity * .10 / (entry * (1 + fee)))
    lot = filters["LOT_SIZE"]
    step = Decimal(lot["stepSize"])
    qty = float((Decimal(str(desired)) / step).to_integral_value(rounding=ROUND_DOWN) * step)
    market = filters.get("MARKET_LOT_SIZE", {})
    maximum = min(float(lot["maxQty"]), float(market.get("maxQty", lot["maxQty"])))
    qty = min(qty, float((Decimal(str(maximum)) / step).to_integral_value(rounding=ROUND_DOWN) * step))
    minimum = max(float(lot["minQty"]), float(market.get("minQty", 0)))
    notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    if qty < minimum or qty <= 0 or qty * raw < float(notional.get("minNotional", 0)):
        return 0.0, entry, stop
    return qty, entry, stop


def simulate(markets, specs, choices, start, end, factor=1, initial=.007):
    first = next(iter(markets.values()))
    indices = np.flatnonzero((first.t >= start) & (first.t < end))
    if not len(indices):
        raise ValueError("Empty spot research period")
    fee, slip, stop_slip = .001 * factor, .0003 * factor, .0005 * factor
    cash, position = initial, None
    curve, trades, skips = [], [], 0

    def sell(raw, ts, reason, slippage):
        nonlocal cash, position
        px = raw * (1 - slippage)
        proceeds = position["qty"] * px * (1 - fee)
        cash += proceeds
        trades.append({"symbol": position["symbol"], "entry_ts": position["entry_ts"],
                       "exit_ts": ts, "reason": reason, "qty": position["qty"],
                       "entry_cost_btc": position["cost"], "exit_proceeds_btc": proceeds,
                       "net_btc": proceeds - position["cost"],
                       "fee_btc": position["fee"] + position["qty"] * px * fee})
        position = None

    for i in indices:
        t = float(first.t[i])
        stopped = False
        # Overnight gap is observed at this open, before the scheduled rotation.
        if position is not None:
            bar = markets[position["symbol"]]
            if float(bar.o[i]) <= position["stop"]:
                sell(float(bar.o[i]), t, "gap_stop", stop_slip)
                stopped = True
        if i in choices and not stopped:
            desired = choices[i]
            if position is not None and position["symbol"] != desired:
                sell(float(markets[position["symbol"]].o[i]), t, "weekly_rotation", slip)
            if position is None and desired is not None:
                raw = float(markets[desired].o[i])
                qty, price, stop = quantity_for_budget(raw, cash, specs[desired], fee, slip, stop_slip)
                if qty:
                    cost = qty * price * (1 + fee)
                    position = {"symbol": desired, "qty": qty, "entry_ts": t, "stop": stop,
                                "cost": cost, "fee": qty * price * fee}
                    cash -= cost
                else:
                    skips += 1
        if position is not None:
            bar = markets[position["symbol"]]
            if float(bar.l[i]) <= position["stop"]:
                sell(min(float(bar.o[i]), position["stop"]), t + PERIOD / 2, "stop", stop_slip)
        equity = cash if position is None else cash + position["qty"] * markets[position["symbol"]].c[i]
        if cash < -1e-12:
            raise AssertionError("Unlevered spot cash went negative")
        curve.append(float(equity))
    if position is not None:
        sell(float(markets[position["symbol"]].c[indices[-1]]), end, "end_of_data", slip)
        curve[-1] = cash
    if not math.isclose(initial + sum(t["net_btc"] for t in trades), cash, abs_tol=1e-12):
        raise AssertionError("Spot BTC ledger does not reconcile")
    return {"n": len(trades), "initial_btc": initial, "final_btc": cash,
            "btc_return": cash / initial - 1, "btc_max_drawdown": max_drawdown([initial, *curve])[0],
            "fees_btc": sum(t["fee_btc"] for t in trades), "skipped_minimum": skips,
            "exit_reasons": dict(Counter(t["reason"] for t in trades)),
            "trades": trades, "equity_btc": curve, "bar_times": first.t[indices].tolist()}


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    protocol = {"created_at": datetime.now(timezone.utc).isoformat(), "symbols": SYMBOLS,
                "lookback_days": LOOKBACKS, "initial_btc": .007, "splits": SPLITS,
                "rule": "Weekly Monday 00 UTC: buy highest positive BTC-pair momentum; otherwise hold BTC. "
                        "No rebalance if same winner; 5% fixed price stop rounded down to tick; max10% entry allocation; "
                        "planned fee/slippage-inclusive stop loss<=0.5% BTC equity. No leverage.",
                "fee_per_side": .001, "slippage": .0003, "stop_slippage": .0005,
                "known_total_trials_at_least": 34, "new_rule_trials": 2,
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "limitations": ["Exploratory extension after previous results; not blind OOS.",
                    "Fixed present-day universe has survivorship/selection bias.",
                    "Current lot/notional filters; fee tier, order book and outages not replayed.",
                    "4h stop timing is approximate; gaps can exceed planned 0.5% risk.",
                    "Maximum drawdown samples 4h closes; intrabar drawdown can be larger.",
                    "Trading fees modeled in BTC equivalent, not actual commission asset.",
                    "No live eligibility, no forward paper samples."]}
    write_json(output / "protocol.json", protocol)
    markets, specs = load_public_data(output)
    write_json(output / "data_manifest.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                              for p in sorted((output / "cache").iterdir())})
    results = {}
    for days in LOOKBACKS:
        choices = weekly_choices(markets, days)
        name = f"spot_btc_momentum_{days}d"
        results[name] = {}
        for period, (start, end) in SPLITS.items():
            results[name][period] = {}
            for cost, factor in (("base", 1), ("stress_2x", 2)):
                value = simulate(markets, specs, choices, start, end, factor)
                write_json(output / f"{name}_{period}_{cost}_audit.json", value)
                results[name][period][cost] = {k:v for k,v in value.items()
                                             if k not in ("trades", "equity_btc", "bar_times")}
            print(name, period, results[name][period], flush=True)
    write_json(output / "results.json", {"protocol": protocol, "results": results})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PACKAGE / "state" / "spot_rotation_research_20260924")
    run(parser.parse_args().output)
