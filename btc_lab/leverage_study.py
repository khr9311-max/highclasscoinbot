"""How much leverage maximizes BTC for the live COIN-M trend rule, liquidation included?

The whole stake follows the live signal (4h EMA20/80 sign of BTCUSD_PERP,
window_ema as btc_portfolio) with fixed leverage L chosen at each entry:
contracts = floor(L * equity_btc * price / 100) on isolated margin equal to the
whole equity. A protective stop sits before liquidation at the nearer of 12%
and 80% of the liquidation distance; after a stop the same side waits for the
opposite signal (live rule). Liquidation (mark price 5m high/low, maintenance
0.4%) loses the margin. Taker fee 0.05% + 3 bps per side, stop fills with the
replay's extra slippage, funding from the cache. No kill switch.

Reports the full 2021-07..2026-09 path and every 12-month window starting on
the first of each month, from 0.003 BTC.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

from btc_lab import aggressive_backtest as ab
from btc_lab import strategy_search as ss

OUTPUT = ab.ROOT / "btc_lab/state/leverage_study_20260926"
LEVERAGES = (1, 2, 3, 5, 10, 20, 50)
MMR = 0.004
START_BTC = 0.003
FULL = ("2021-07-20", "2026-09-24")


def liquidation_price(entry, side, contracts, margin, mmr=MMR):
    """Inverse contract, isolated margin: price where margin + P&L = maintenance."""
    notional = contracts * 100
    if side > 0:
        return (1 + mmr) * notional / (margin + notional / entry)
    denom = notional / entry - margin
    return (1 - mmr) * notional / denom if denom > 0 else math.inf


def run(d, lev, start, end):
    fee = ab.FEE_COIN + ab.SLIP
    a = -(-((ss.stamp(start) - d.t0) // 300_000) // ab.B4) * ab.B4
    b = min((ss.stamp(end) - d.t0) // 300_000, d.n)
    equity, side, qty, entry, stop, liq, blocked = START_BTC, 0, 0, None, None, None, None
    days, values, liquidations, stops = [], [], 0, 0
    for i in range(a // ab.B4, b // ab.B4):
        t = i * ab.B4
        if t % ab.BD == 0:
            mark_value = equity if not side else equity + qty * 100 * side * (1 / entry - 1 / d.mark["open"][t])
            days.append(t // ab.BD)
            values.append(mark_value)
        if equity <= 0:
            break
        direction = d.direction(i)
        if side and direction and direction != side:            # flip: close at the open
            px = d.coin["open"][t] * (1 - side * ab.SLIP)
            equity += qty * 100 * side * (1 / entry - 1 / px) - qty * 100 / px * ab.FEE_COIN
            side, qty, blocked = 0, 0, None
        if not side and direction and blocked != direction:
            px = d.coin["open"][t] * (1 + direction * ab.SLIP)
            q = math.floor(lev * equity * px / 100 + 1e-9)
            if q >= 1:
                equity -= q * 100 / px * ab.FEE_COIN
                side, qty, entry = direction, q, px
                liq = liquidation_price(px, side, qty, equity)
                dist = abs(liq / px - 1) if math.isfinite(liq) else 1.0
                s = min(0.12, 0.8 * dist)
                stop = px * (1 - side * s)
                blocked = None
        if not side:
            continue
        k = np.arange(t, min(t + ab.B4, b))
        mo, mh, ml = d.mark["open"][k], d.mark["high"][k], d.mark["low"][k]
        if side > 0:
            liq_hit, stop_hit = mo <= liq, ml <= stop
        else:
            liq_hit, stop_hit = mo >= liq, mh >= stop
        event = liq_hit | stop_hit
        first = int(np.argmax(event)) if event.any() else len(k)
        for j in np.flatnonzero(d.funding[k[:first]]):
            equity -= side * qty * 100 / mo[j] * d.funding[k[j]]
        if first == len(k):
            continue
        if liq_hit[first]:                                       # gapped through the stop
            equity, liquidations = 0.0, liquidations + 1
        else:
            opening = d.coin["open"][k[first]]
            fill = (min(stop, opening) * (1 - ab.STOP_SLIP) if side > 0 else max(stop, opening) * (1 + ab.STOP_SLIP))
            equity += qty * 100 * side * (1 / entry - 1 / fill) - qty * 100 / fill * ab.FEE_COIN
            stops += 1
            blocked = side
        side, qty = 0, 0
    final = equity if not side else equity + qty * 100 * side * (1 / entry - 1 / d.coin["close"][b - 1])
    values.append(max(final, 0.0))
    v = np.maximum(np.array(values), 0.0)
    peak = np.maximum.accumulate(v)
    dd = float(np.max(1 - np.where(peak > 0, v / peak, 1.0)))
    return {"multiple": float(v[-1] / START_BTC), "max_dd": dd, "liquidations": liquidations, "stops": stops}


def study(output=OUTPUT):
    d = ab.Data()
    out = {"full": {}, "rolling_12m": {}}
    starts = pd.date_range("2021-08-01", "2025-09-01", freq="MS")
    for lev in LEVERAGES:
        out["full"][lev] = run(d, lev, *FULL)
        mult = []
        for s in starts:
            e = (s + pd.DateOffset(months=12)).strftime("%Y-%m-%d")
            mult.append(run(d, lev, s.strftime("%Y-%m-%d"), e)["multiple"])
        m = np.array(mult)
        out["rolling_12m"][lev] = {"windows": len(m), "median": float(np.median(m)), "mean": float(m.mean()),
                                   "p10": float(np.percentile(m, 10)), "p90": float(np.percentile(m, 90)),
                                   "best": float(m.max()), "p_double": float(np.mean(m >= 2)),
                                   "p_5x": float(np.mean(m >= 5)), "p_lose_half": float(np.mean(m <= 0.5)),
                                   "p_lose_90": float(np.mean(m <= 0.1))}
        f, r = out["full"][lev], out["rolling_12m"][lev]
        print(f"{lev:3d}x | full 2021-07..2026-09: x{f['multiple']:.3g} maxDD {f['max_dd']*100:.0f}% liq {f['liquidations']} stops {f['stops']} | "
              f"1-year windows: median x{r['median']:.2f} mean x{r['mean']:.2f} p10 x{r['p10']:.2f} p90 x{r['p90']:.2f} best x{r['best']:.1f} "
              f"P(>=2x) {r['p_double']*100:.0f}% P(>=5x) {r['p_5x']*100:.0f}% P(<=half) {r['p_lose_half']*100:.0f}% P(-90%) {r['p_lose_90']*100:.0f}%",
              flush=True)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(out, indent=1))
    return out


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    study()


if __name__ == "__main__":
    main()
