"""Public-data-only, fixed-rule COIN-M research, valued in a common BTC unit.

Never loads .env, creates orders, or writes validation approval. Simulator fields
ending in ``btc`` are generic inverse-contract collateral units internally; this
module explicitly relabels them before saving non-BTC results.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from ..config.settings import Settings
from ..exchange.contract import resolve_contract
from ..exchange.market_data import MarketData, closed_only, find_gaps, klines_to_bars
from ..exchange.rest_client import BinanceRestClient, LIVE_REST
from .data import (Dataset, cache_dir, load_dataset, load_funding, load_klines,
                   save_funding, save_klines)
from .metrics import max_drawdown
from .research import iso, timestamp, window, write_json
from .research_signals import CANDIDATES, build_precomputed
from .runner import sim_config

PACKAGE = Path(__file__).resolve().parents[1]
SYMBOLS = ("BTCUSD_PERP", "ETHUSD_PERP", "BNBUSD_PERP", "SOLUSD_PERP", "XRPUSD_PERP")
CANDIDATE = next(c for c in CANDIDATES if c.name == "ema_cross_4h")
DOWNLOAD_START = "2023-11-01"
CONVERSION_COST = 0.002  # Each direction: BTC->USDT->coin, 2 assumed 0.1% legs.


def aligned_prices(bars, times, field="c"):
    """Require exact timestamps; never forward-fill an asset conversion price."""
    times = np.asarray(times)
    idx = np.searchsorted(bars.t, times)
    if len(times) and (np.any(idx >= len(bars)) or not np.array_equal(bars.t[idx], times)):
        raise ValueError("Missing matching BTC valuation timestamps")
    prices = np.asarray(getattr(bars, field)[idx], dtype=float)
    if not np.isfinite(prices).all() or np.any(prices <= 0):
        raise ValueError("Invalid valuation prices")
    return prices


def collateral_for_btc(initial_btc, btc_price, asset_price, conversion_cost):
    vals = (initial_btc, btc_price, asset_price)
    if not all(math.isfinite(v) and v > 0 for v in vals):
        raise ValueError("Positive finite initial BTC and prices required")
    if not math.isfinite(conversion_cost) or not 0 <= conversion_cost < 1:
        raise ValueError("Conversion cost must be in [0, 1)")
    # Simulator's Decimal bridge expects native numeric repr, not np.float64(...).
    return float(initial_btc * btc_price / asset_price * (1 - conversion_cost))


def btc_curve(collateral_curve, asset_prices, btc_prices, conversion_cost):
    """Net liquidatable BTC at every time, including assumed final conversion."""
    collateral_curve, asset_prices, btc_prices = map(
        lambda a: np.asarray(a, dtype=float), (collateral_curve, asset_prices, btc_prices))
    if collateral_curve.shape != asset_prices.shape or asset_prices.shape != btc_prices.shape:
        raise ValueError("Valuation arrays must align")
    if (not all(np.isfinite(a).all() for a in (collateral_curve, asset_prices, btc_prices))
            or np.any(asset_prices <= 0) or np.any(btc_prices <= 0)):
        raise ValueError("Non-finite balance or invalid price")
    if not math.isfinite(conversion_cost) or not 0 <= conversion_cost < 1:
        raise ValueError("Conversion cost must be in [0, 1)")
    return collateral_curve * asset_prices / btc_prices * (1 - conversion_cost)


def load_asset_dataset(state, symbol):
    """Research-only loader with explicit collateral identity, strict data checks."""
    c = cache_dir(str(state))
    asset = symbol.removesuffix("USD_PERP")
    spec = resolve_contract(json.loads((c / f"exchange_info_{symbol}.json").read_text()),
                            symbol, margin_asset=asset, base_asset=asset)
    series = {}
    for interval, kind, period in (("1h", "contract", 3600), ("4h", "contract", 14400),
                                   ("1h", "mark", 3600)):
        rows = load_klines(c / f"{symbol}_{interval}_{kind}.csv")
        if not rows or find_gaps(rows, period):
            raise ValueError(f"Empty or discontinuous {symbol} {interval} {kind}")
        bars = klines_to_bars(rows, period)
        values = np.array([bars.o, bars.h, bars.l, bars.c])
        if (not np.isfinite(values).all() or np.any(values <= 0)
                or np.any(bars.h < np.maximum(bars.o, bars.c))
                or np.any(bars.l > np.minimum(bars.o, bars.c))):
            raise ValueError(f"Invalid OHLC {symbol} {interval} {kind}")
        series[(interval, kind)] = bars
    ltf, mark = series[("1h", "contract")], series[("1h", "mark")]
    if not np.array_equal(ltf.t, mark.t):
        raise ValueError(f"Unaligned contract/mark timestamps: {symbol}")
    funding = load_funding(c / f"{symbol}_funding.csv", symbol)
    if not funding:
        raise ValueError(f"Missing funding history {symbol}")
    return Dataset(symbol, ltf, series[("4h", "contract")], mark, 0, funding, spec)


async def download_assets(output, end, log=print):
    """Sequential public GETs only; bounded pages and pauses respect API weights."""
    c = cache_dir(str(output))
    rest = BinanceRestClient(LIVE_REST)  # No environment, credentials or signed calls.
    try:
        md = MarketData(rest)
        server_ms = await md.server_time_ms()
        end_ms = min(int(end * 1000) - 1, server_ms - 1)
        info = await md.exchange_info()
        for symbol in SYMBOLS[1:]:
            asset = symbol.removesuffix("USD_PERP")
            resolve_contract(info, symbol, margin_asset=asset, base_asset=asset)
            raw = next(s for s in info["symbols"] if s["symbol"] == symbol)
            write_json(c / f"exchange_info_{symbol}.json", {"symbols": [raw], "serverTime": server_ms})
            for interval, kind, step in (("1h", "contract", 3600000),
                                         ("4h", "contract", 14400000),
                                         ("1h", "mark", 3600000)):
                path = c / f"{symbol}_{interval}_{kind}.csv"
                have = load_klines(path)
                cursor = have[-1].open_time_ms + step if have else int(timestamp(DOWNLOAD_START) * 1000)
                log(f"Download {symbol} {interval} {kind}", flush=True)
                while cursor <= end_ms:
                    # 1,000-bar pages weigh less than the 1,500-bar maximum.
                    finish = min(cursor + 1000 * step - 1, end_ms)
                    have.extend(await md.klines(symbol, interval, limit=1000,
                                                start_ms=cursor, end_ms=finish, kind=kind))
                    cursor = finish + 1
                    await asyncio.sleep(0.30)
                have = closed_only(have, min(server_ms, int(end * 1000)))
                save_klines(path, have)
                log(f"  {len(have)} bars, {len(find_gaps(have, step // 1000))} gaps", flush=True)
            path = c / f"{symbol}_funding.csv"
            have = load_funding(path, symbol)
            cursor = have[-1].funding_time_ms + 1 if have else int(timestamp(DOWNLOAD_START) * 1000)
            have.extend(await md.funding_history(symbol, cursor, end_ms))
            have = [f for f in have if f.funding_time_ms <= end_ms]
            save_funding(path, have)
            log(f"  {symbol} funding {len(have)} records", flush=True)
            await asyncio.sleep(0.30)
    finally:
        await rest.close()


def describe(ds, btc_ds, result, initial_btc, conversion_cost):
    """Report both collateral trading PnL and total common-BTC wallet PnL."""
    if not len(result.equity_ts):
        raise ValueError("No result valuation timestamps")
    asset_prices = aligned_prices(ds.mark, result.equity_ts)
    btc_prices = aligned_prices(btc_ds.mark, result.equity_ts)
    initial_asset = result.config.start_equity_btc
    strategy = btc_curve(result.equity_curve, asset_prices, btc_prices, conversion_cost)
    passive = btc_curve(np.full(len(asset_prices), initial_asset), asset_prices,
                        btc_prices, conversion_cost)
    if not math.isclose(initial_asset + sum(t["net_btc"] for t in result.trades),
                        result.final_equity, rel_tol=1e-10, abs_tol=1e-12):
        raise AssertionError("Collateral ledger does not reconcile")
    net = np.array([t["net_btc"] for t in result.trades])
    return {
        "margin_asset": ds.spec.margin_asset,
        "initial_btc": initial_btc, "initial_collateral_after_conversion": initial_asset,
        "final_collateral": result.final_equity,
        "collateral_trading_return": result.final_equity / initial_asset - 1,
        "final_btc_after_conversion": float(strategy[-1]),
        "btc_total_return": float(strategy[-1] / initial_btc - 1),
        "btc_max_drawdown": max_drawdown(np.r_[initial_btc, strategy])[0],
        "passive_same_asset_final_btc": float(passive[-1]),
        "passive_same_asset_btc_return": float(passive[-1] / initial_btc - 1),
        "passive_same_asset_btc_max_drawdown": max_drawdown(np.r_[initial_btc, passive])[0],
        "excess_vs_same_asset_btc_percentage_points": float((strategy[-1] - passive[-1]) / initial_btc * 100),
        "n_trades": len(result.trades), "n_skipped": len(result.skipped),
        "win_rate_collateral": float((net > 0).mean()) if len(net) else None,
        "fees_collateral": sum(t["fee_btc"] for t in result.trades),
        "funding_collateral": result.funding_total,
        # Sizing messages internally say BTC: never reproduce that false denomination.
        "skip_reason_counts": dict(Counter(t.get("skip_reason", "").split("(")[0].strip()
                                           for t in result.skipped)),
        "conversion_cost_each_way": conversion_cost,
        "first_valuation_open_utc": iso(result.equity_ts[0]),
        "last_valuation_close_utc": iso(result.equity_ts[-1] + ds.ltf.period),
    }


def run(data_state, output, initial_btc=0.007, download=True, log=print):
    if not math.isfinite(initial_btc) or initial_btc <= 0:
        raise ValueError("initial_btc must be finite and positive")
    output = Path(output)
    if output.resolve() == Path(data_state).resolve():
        raise ValueError("Use a separate research output folder")
    output.mkdir(parents=True, exist_ok=True)
    btc_ds = load_dataset(str(data_state), SYMBOLS[0])
    end = float(btc_ds.ltf.t[-1]) + btc_ds.ltf.period
    periods = {"validation_2024": (timestamp("2024-01-01"), timestamp("2025-01-01")),
               "recent_2025_onward": (timestamp("2025-01-01"), end)}
    protocol = {
        "created_before_new_results": True, "historical_data_previously_viewed": True,
        "purpose": "Exploratory fixed-rule cross-asset comparison, not blind OOS or live approval",
        "symbols": list(SYMBOLS), "candidate": asdict(CANDIDATE),
        "periods": {k: [iso(v[0]), iso(v[1])] for k, v in periods.items()},
        "warmup_begin": DOWNLOAD_START, "initial_btc_per_independent_case": initial_btc,
        "new_non_btc_rule_market_trials": 4,
        "known_prior_rule_trials": 21, "parallel_timeframe_rule_trials": 7,
        "known_total_trials_at_least": 32,
        "selection": "No winner promotion; all five fixed markets reported, no recent-window retuning",
        "conversion": "Mark USD ratio proxy, 0.2% each way for non-BTC (two assumed 0.1% spot legs); BTC zero. Not actual executable spread or fee-tier quote.",
        "cost_stress": "2x taker fee, entry/stop slippage AND conversion costs; position size recalculated",
        "risk": "0.5% per trade in collateral coin units; excludes collateral/BTC price risk. No complete BTC-risk cap.",
        "limitations": [
            "EMA4h chosen after prior research; viewed history is exploratory, not fresh independent validation.",
            "Each period starts flat with fresh equal BTC capital; all positions closed at the end.",
            "No order book depth, latency, liquidation/ADL or outage replay; current contract filters only.",
            "One position per isolated case; these results cannot be summed into a shared-capital portfolio.",
            "Non-BTC collateral changes value against BTC while idle as well as while trading.",
            "Surviving five currently traded assets; not a historical all-coin unbiased universe.",
            "Mark-based synthetic conversions omit executable spot basis, transfer delays and funding borrowing costs.",
            "Funding with absent mark uses hourly mark open; tiny funding offsets bucket to hour.",
        ],
        "source_sha256": {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    }
    write_json(output / "protocol.json", protocol)
    if download:
        asyncio.run(download_assets(output, end, log))
    base = sim_config(Settings.build(), CANDIDATE.variant, (1, -1), 1.0)
    cfg = replace(base, max_hold_bars=CANDIDATE.max_hold_bars, valid_bars=CANDIDATE.valid_bars)
    report = {"protocol": protocol, "results": {}, "live_eligible": False}
    for symbol in SYMBOLS:
        ds = btc_ds if symbol == SYMBOLS[0] else load_asset_dataset(output, symbol)
        pre = build_precomputed(ds, CANDIDATE)
        row = {"margin_asset": ds.spec.margin_asset, "contract": ds.spec.essentials(),
               "signals": pre.signal_count(), "data": {
                   "first": iso(ds.ltf.t[0]), "end": iso(ds.ltf.t[-1] + ds.ltf.period),
                   "mark_missing": ds.mark_missing, "funding_records": len(ds.funding),
                   "funding_missing_mark": sum(f.mark_price is None for f in ds.funding)},
               "periods": {}}
        for period, (start, finish) in periods.items():
            sim = window(ds, pre, start, finish)
            asset_open = aligned_prices(ds.mark, [start], "o")[0]
            btc_open = aligned_prices(btc_ds.mark, [start], "o")[0]
            row["periods"][period] = {}
            for cost, factor in (("base", 1), ("stress_2x", 2)):
                fee = 0.0 if ds.spec.margin_asset == "BTC" else CONVERSION_COST * factor
                equity = collateral_for_btc(initial_btc, btc_open, asset_open, fee)
                config = replace(cfg, start_equity_btc=equity,
                                 taker_fee=cfg.taker_fee * factor,
                                 slippage_bps=cfg.slippage_bps * factor,
                                 stop_slippage_bps=cfg.stop_slippage_bps * factor)
                result = sim.run(config)
                summary = describe(ds, btc_ds, result, initial_btc, fee)
                row["periods"][period][cost] = summary
                log(f"{symbol} {period} {cost}: BTC {summary['btc_total_return']:.2%}, "
                    f"collateral {summary['collateral_trading_return']:.2%}, "
                    f"trades {summary['n_trades']}", flush=True)
        report["results"][symbol] = row
        write_json(output / "report.json", report)
    report["data_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for folder in (cache_dir(str(output)), cache_dir(str(data_state)))
                             for p in sorted(folder.iterdir()) if p.is_file()}
    write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-state", type=Path, default=PACKAGE / "state")
    parser.add_argument("--output", type=Path, default=PACKAGE / "state" / "multiasset_research_20260924")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    run(args.data_state, args.output, download=not args.offline)


if __name__ == "__main__":
    main()
