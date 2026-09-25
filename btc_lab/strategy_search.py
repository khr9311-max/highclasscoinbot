"""Pre-registered strategy search on 5-minute history, measured in BTC.

Every candidate below is fixed in code before the holdout is read. Candidates
are ranked on the development period only; the holdout is reported for the
chosen few, the implemented intraday rule and simple references.

Venues and BTC accounting:
- ALTS: ETHBTC, BNBBTC, SOLBTC, XRPBTC spot, long or flat against BTC. Four
  independent sleeves of equal weight, rebalanced daily (rotation candidates
  use one sleeve that holds the chosen alt).
- COINM: BTCUSD_PERP inverse perpetual, BTC collateral. Returns per unit BTC
  notional: long 1 - entry/exit, short entry/exit - 1, funding included.

Causality: indicators use completed bars only (shifted by one bar); higher
timeframe regimes are available from the close of their bar. Breakout
entries trigger inside the bar at the level (or the open after a gap); stops
are checked in the entry bar and, when a stop and a target share a later bar,
the stop is assumed first. Costs per side: taker fee + 3 bps + half a tick.
Contract prices stand in for the mark price. The intraday baseline evaluates
each alt independently instead of choosing a single alt.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / "btc_lab/state/intraday_market"
FUNDING = ROOT / "binance_coinm_v1/state/cache/BTCUSD_PERP_funding.csv"
OUTPUT = ROOT / "btc_lab/state/strategy_search_20260925"
ALTS = ("ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC")
TICK = {"ETHBTC": 1e-5, "BNBBTC": 1e-6, "SOLBTC": 1e-7, "XRPBTC": 1e-8, "BTCUSD_PERP": 0.1}
FEE = {"spot": 0.001, "coinm": 0.0005}
SLIP = 0.0003
TF = {"5m": 1, "15m": 3, "1h": 12, "4h": 48, "1d": 288}
DAY_BARS = 288
DEV = ("2021-07-20", "2024-07-01")
HOLDOUT = ("2024-07-01", "2026-09-25")


def stamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- data

class Market:
    def __init__(self, directory=MARKET):
        raw = {name: np.load(directory / f"{name}_5m.npz") for name in (*ALTS, "BTCUSD_PERP")}
        start = max(int(z["open_ms"][0]) for z in raw.values())
        end = min(int(z["open_ms"][-1]) for z in raw.values()) + 300_000
        start, end = start // 86_400_000 * 86_400_000, end // 86_400_000 * 86_400_000
        self.t0 = start
        self.n = (end - start) // 300_000
        grid = start + 300_000 * np.arange(self.n, dtype=np.int64)
        self.series = {}
        for name, z in raw.items():
            frame = pd.DataFrame({k: z[k] for k in ("open", "high", "low", "close", "volume")},
                                 index=z["open_ms"]).reindex(grid)
            close = frame["close"].ffill().bfill()
            for k in ("open", "high", "low"):
                frame[k] = frame[k].fillna(close)
            frame["close"] = close
            frame["volume"] = frame["volume"].fillna(0.0)
            self.series[name] = {k: frame[k].to_numpy() for k in frame}
        self.funding = np.zeros(self.n)
        with open(FUNDING, newline="") as handle:
            rows = csv.reader(handle)
            next(rows)
            for r in rows:
                i = (int(r[0]) - start) // 300_000
                if 0 <= i < self.n:
                    self.funding[i] += float(r[1])
        self._bars = {}

    def bars(self, name, tf):
        key = (name, tf)
        if key not in self._bars:
            k = TF[tf]
            s = self.series[name]
            m = self.n // k
            cut = m * k
            self._bars[key] = {
                "open": s["open"][:cut:k].copy(),
                "high": s["high"][:cut].reshape(m, k).max(1),
                "low": s["low"][:cut].reshape(m, k).min(1),
                "close": s["close"][k - 1:cut:k].copy(),
                "volume": s["volume"][:cut].reshape(m, k).sum(1),
                "funding": self.funding[:cut].reshape(m, k).sum(1),
                "k": k,
            }
        return self._bars[key]

    def index(self, text, k):
        return (stamp(text) - self.t0) // 300_000 // k


def ema(values, span):
    return pd.Series(values).ewm(span=span, adjust=False).mean().to_numpy()


def prior(values):
    """Value known at the open of each bar: the previous bar's completed value."""
    out = np.empty_like(values, dtype=float)
    out[0] = np.nan
    out[1:] = values[:-1]
    return out


def simple_rsi(close, n=14):
    """The intraday module's RSI: gain share of the last n changes (not Wilder)."""
    change = np.diff(close, prepend=np.nan)
    gain = pd.Series(np.clip(change, 0, None)).rolling(n).sum().to_numpy()
    loss = pd.Series(np.clip(-change, 0, None)).rolling(n).sum().to_numpy()
    total = gain + loss
    return np.where(total > 0, 100 * gain / np.where(total > 0, total, 1), 50.0)


def atr(bars, n=14):
    c = bars["close"]
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.nanmax(np.vstack([bars["high"] - bars["low"], np.abs(bars["high"] - pc), np.abs(bars["low"] - pc)]), 0)
    return pd.Series(tr).rolling(n).mean().to_numpy()


def regime(market, name, tf, htf):
    """Closed higher-timeframe EMA20/50 trend mapped onto the lower timeframe."""
    hb = market.bars(name, htf)
    fast, slow, close = ema(hb["close"], 20), ema(hb["close"], 50), hb["close"]
    bull = (fast > slow) & (close > slow)
    bear = (fast < slow) & (close < slow)
    bull[:50], bear[:50] = False, False
    k, hk = TF[tf], TF[htf]
    m = len(market.bars(name, tf)["close"])
    src = (np.arange(m) * k) // hk - 1
    valid = src >= 0
    src = np.clip(src, 0, len(bull) - 1)
    return np.where(valid, bull[src], False), np.where(valid, bear[src], False)


# ---------------------------------------------------------------- trade engine

@dataclass
class Trades:
    entry_bar: np.ndarray
    exit_bar: np.ndarray
    ret: np.ndarray
    k: int


def per_side_cost(name, price, mult):
    venue = "coinm" if name == "BTCUSD_PERP" else "spot"
    return (FEE[venue] + SLIP) * mult + 0.5 * TICK[name] / price * mult


def trade_return(name, side, entry, exit_, fund, mult):
    fee = FEE["coinm" if name == "BTCUSD_PERP" else "spot"] * mult
    slip_e = SLIP * mult + 0.5 * TICK[name] / entry * mult
    slip_x = SLIP * mult + 0.5 * TICK[name] / exit_ * mult
    if name == "BTCUSD_PERP":
        pe, px = entry * (1 + side * slip_e), exit_ * (1 - side * slip_x)
        gross = (1 - pe / px) if side > 0 else (pe / px - 1)
        return gross - fee - fee * pe / px - side * fund
    return exit_ * (1 - slip_x) * (1 - fee) / (entry * (1 + slip_e) / (1 - fee)) - 1


def run_events(name, bars, side, signal, entry_price, stop_frac, *, tp_mult=None, trail=None,
               exit_open=None, stop_in_entry_bar=True, max_hold=48, cooldown=1, cost_mult=1.0):
    """Sequential non-overlapping trades.

    signal[i]: enter during bar i at entry_price[i]; stop_frac[i]: stop distance.
    trail[j]: adverse exit level active in bar j (NaN if none).
    exit_open[j]: leave at the open of bar j (regime change or close-based rule).
    """
    o, h, low = bars["open"], bars["high"], bars["low"]
    fund = np.cumsum(bars["funding"])
    n = len(o)
    candidates = np.flatnonzero(signal)
    entries, exits, rets = [], [], []
    allowed, k = 0, 0
    while k < len(candidates):
        i = candidates[k]
        if i < allowed:
            k = np.searchsorted(candidates, allowed)
            continue
        if i + max_hold >= n:
            break
        e = entry_price[i]
        sf = stop_frac[i]
        stop = e * (1 - side * sf)
        tp = e * (1 + side * tp_mult * sf) if tp_mult else None
        x = xb = None
        if stop_in_entry_bar and (low[i] <= stop if side > 0 else h[i] >= stop):
            x, xb = stop, i
        else:
            js = slice(i + 1, i + max_hold)
            oo, hh, ll = o[js], h[js], low[js]
            if side > 0:
                adverse = np.fmax(stop, trail[js]) if trail is not None else np.full(len(oo), stop)
                c_open = oo <= adverse
                c_adv = ll <= adverse
                c_tpo = oo >= tp if tp else np.zeros(len(oo), bool)
                c_tp = hh >= tp if tp else np.zeros(len(oo), bool)
            else:
                adverse = np.fmin(stop, trail[js]) if trail is not None else np.full(len(oo), stop)
                c_open = oo >= adverse
                c_adv = hh >= adverse
                c_tpo = oo <= tp if tp else np.zeros(len(oo), bool)
                c_tp = ll <= tp if tp else np.zeros(len(oo), bool)
            c_exit = exit_open[js] if exit_open is not None else np.zeros(len(oo), bool)
            any_hit = c_open | c_adv | c_tpo | c_tp | c_exit
            if any_hit.any():
                j = int(np.argmax(any_hit))
                xb = i + 1 + j
                if c_exit[j] or c_open[j] or c_tpo[j]:
                    x = oo[j]
                elif c_adv[j]:
                    x = adverse[j]
                else:
                    x = tp
            else:
                xb = i + max_hold
                x = o[xb]
        funding = fund[xb] - fund[i] if name == "BTCUSD_PERP" else 0.0
        entries.append(i)
        exits.append(xb)
        rets.append(trade_return(name, side, e, x, funding, cost_mult))
        allowed = xb + cooldown
        k += 1
    return Trades(np.array(entries, int), np.array(exits, int), np.array(rets), bars["k"])


def run_positions(name, bars, pos, cost_mult=1.0):
    """Target position (-1/0/1) decided at the prior close, held over each bar."""
    c = bars["close"]
    pos = np.nan_to_num(pos).astype(float)
    prev = np.concatenate(([c[0]], c[:-1]))
    if name == "BTCUSD_PERP":
        move = 1 - prev / c
        r = pos * move - pos * bars["funding"]
    else:
        r = pos * (c / prev - 1)
    change = np.abs(np.diff(pos, prepend=0.0))
    r = r - change * per_side_cost(name, c, cost_mult)
    return r, pos


def position_trades(r, pos, k):
    """Split a position path into trades (contiguous same-sign holdings)."""
    sign = np.sign(pos)
    starts = np.flatnonzero((sign != 0) & (np.diff(sign, prepend=0) != 0))
    entries, exits, rets = [], [], []
    for s in starts:
        e = s
        while e + 1 < len(sign) and sign[e + 1] == sign[s]:
            e += 1
        entries.append(s)
        exits.append(e)
        rets.append(np.prod(1 + r[s:e + 1]) - 1)
    return Trades(np.array(entries, int), np.array(exits, int), np.array(rets), k)


# ---------------------------------------------------------------- candidates

@dataclass
class Candidate:
    family: str
    venue: str          # alts | coinm
    params: dict = field(default_factory=dict)

    @property
    def name(self):
        p = ",".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.family}[{p}]"


def breakout(market, name, c, side, cost_mult):
    tf, n = c.params["tf"], c.params["lookback"]
    htf = "4h" if tf == "1h" else "1h"
    b = market.bars(name, tf)
    close = b["close"]
    f9, s21 = prior(ema(close, 9)), prior(ema(close, 21))
    rsi = prior(simple_rsi(close))
    a = prior(atr(b))
    vol = b["volume"]
    avg = pd.Series(vol).shift(1).rolling(20).mean().to_numpy()   # 20 bars before the last completed
    vratio = prior(vol / np.where(avg > 0, avg, np.nan))
    hh = pd.Series(b["high"]).rolling(n).max().shift(1).to_numpy()
    ll = pd.Series(b["low"]).rolling(n).min().shift(1).to_numpy()
    pc = prior(close)
    raw = 2 * a / pc
    fee = FEE["coinm" if name == "BTCUSD_PERP" else "spot"]
    cost = 2 * fee + TICK[name] / pc + 0.0011
    bull, bear = regime(market, name, tf, htf)
    if side > 0:
        trend, level, touched = bull, hh, b["high"] > hh
        direction = (f9 > s21) & (rsi >= 50) & (rsi <= 75)
    else:
        trend, level, touched = bear, ll, b["low"] < ll
        direction = (f9 < s21) & (rsi >= 25) & (rsi <= 50)
    ok = trend & touched & (raw <= 0.02)
    if c.params["filters"] == "full":
        ok &= direction & (vratio >= 1.2) & (2 * raw >= 3 * cost)
    ok &= ~np.isnan(raw) & ~np.isnan(level)
    entry = np.where(side > 0, np.fmax(b["open"], level), np.fmin(b["open"], level))
    stop = np.fmax(0.003, raw)
    exit_open = ~trend
    tp = 2 if c.params["exit"] == "tp2_ema" else None
    cooldown = max(1, math.ceil(3 / TF[tf]))
    return run_events(name, b, side, ok, entry, stop, tp_mult=tp, trail=s21, exit_open=exit_open,
                      max_hold=48, cooldown=cooldown, cost_mult=cost_mult)


def reversion(market, name, c, side, cost_mult):
    tf, thr = c.params["tf"], c.params["rsi"]
    htf = "4h" if tf == "1h" else "1h"
    b = market.bars(name, tf)
    rsi = prior(simple_rsi(b["close"]))
    a = prior(atr(b))
    pc = prior(b["close"])
    if side > 0:
        signal = rsi < thr
        exit_open = rsi > 50
    else:
        signal = rsi > 100 - thr
        exit_open = rsi < 50
    if c.params["trend"]:
        bull, bear = regime(market, name, tf, htf)
        signal &= bull if side > 0 else bear
    signal &= ~np.isnan(a)
    stop = 3 * a / pc
    return run_events(name, b, side, signal, b["open"], stop, exit_open=exit_open,
                      stop_in_entry_bar=True, max_hold=48, cooldown=1, cost_mult=cost_mult)


def ema_trend(market, name, c, cost_mult):
    b = market.bars(name, c.params["tf"])
    fast, slow = ema(b["close"], c.params["fast"]), ema(b["close"], c.params["slow"])
    raw = np.sign(fast - slow)
    raw[:c.params["slow"]] = 0
    pos = prior(raw)
    if c.params.get("mode", "long_flat") == "long_flat":
        pos = np.clip(pos, 0, None)
    r, pos = run_positions(name, b, pos, cost_mult)
    return r, pos, b["k"]


def ts_momentum(market, name, c, cost_mult):
    b = market.bars(name, "4h")
    lag = c.params["hours"] // 4
    close = b["close"]
    past = np.concatenate((np.full(lag, np.nan), close[:-lag]))
    pos = prior(np.sign(close / past - 1))
    if c.params.get("mode", "long_flat") == "long_flat":
        pos = np.clip(pos, 0, None)
    r, pos = run_positions(name, b, pos, cost_mult)
    return r, pos, b["k"]


def rotation(market, c, cost_mult):
    """One sleeve holding the strongest alt vs BTC, or BTC when none is positive."""
    bars = {s: market.bars(s, "4h") for s in ALTS}
    closes = np.vstack([bars[s]["close"] for s in ALTS])
    m = closes.shape[1]

    def ret(hours):
        lag = hours // 4
        past = np.concatenate((np.full((len(ALTS), lag), np.nan), closes[:, :-lag]), 1)
        return closes / past - 1
    if c.params["lookback"] == "20d60d":
        score = (ret(480) + ret(1440)) / 2
    else:
        score = ret(c.params["lookback"])
    every = c.params["rebalance_h"] // 4
    choice = np.full(m, -1)
    current = -1
    for i in range(1, m):
        if i % every == 0:
            col = score[:, i - 1]
            if np.all(np.isnan(col)):
                current = -1
            else:
                best = int(np.nanargmax(col))
                current = best if col[best] > 0 else -1
        choice[i] = current
    r = np.zeros(m)
    for idx, s in enumerate(ALTS):
        held = (choice == idx).astype(float)
        c_ = closes[idx]
        prev = np.concatenate(([c_[0]], c_[:-1]))
        cost = per_side_cost(s, c_, cost_mult)
        r += held * (c_ / prev - 1) - np.abs(np.diff(held, prepend=0.0)) * cost
    pos = (choice >= 0).astype(float)
    return r, pos, 48


def candidates():
    out = []
    for venue in ("alts", "coinm"):
        for tf in ("5m", "15m", "1h"):
            for lookback in (12, 48):
                for filters in ("full", "trend"):
                    for exit_ in ("tp2_ema", "trail"):
                        out.append(Candidate("breakout", venue, dict(tf=tf, lookback=lookback, filters=filters, exit=exit_)))
        for tf in ("5m", "15m", "1h"):
            for rsi in (25, 30):
                for trend in (False, True):
                    out.append(Candidate("reversion", venue, dict(tf=tf, rsi=rsi, trend=trend)))
        modes = ("long_flat",) if venue == "alts" else ("long_flat", "long_short")
        for tf in ("1h", "4h", "1d"):
            for fast, slow in ((9, 21), (20, 50), (20, 80), (50, 200)):
                for mode in modes:
                    out.append(Candidate("ema_trend", venue, dict(tf=tf, fast=fast, slow=slow, mode=mode)))
        for hours in (24, 72, 168, 720):
            for mode in modes:
                out.append(Candidate("ts_momentum", venue, dict(hours=hours, mode=mode)))
    for lookback in (4, 24, 72, 168, 480, "20d60d"):
        for rebalance_h in (4, 24):
            out.append(Candidate("rotation", "alts", dict(lookback=lookback, rebalance_h=rebalance_h)))
    out.append(Candidate("hold", "alts", dict(kind="equal_weight_alts")))
    out.append(Candidate("hold", "coinm", dict(kind="long_1x")))
    return out


BASELINE = dict(tf="5m", lookback=12, filters="full", exit="tp2_ema")


# ---------------------------------------------------------------- evaluation

def daily_from_bars(r, k, n_days):
    per_day = DAY_BARS // k
    m = n_days * per_day
    r = np.concatenate((r, np.zeros(max(0, m - len(r)))))[:m]
    return np.prod(1 + r.reshape(n_days, per_day), 1) - 1


def daily_from_trades(tr, n_days):
    out = np.ones(n_days)
    days = tr.exit_bar * tr.k // DAY_BARS
    for d, v in zip(days, tr.ret):
        if d < n_days:
            out[d] *= 1 + v
    return out - 1


def exposure_from_trades(tr, n_bars_5m):
    held = np.zeros(n_bars_5m)
    for e, x in zip(tr.entry_bar, tr.exit_bar):
        held[e * tr.k:(x + 1) * tr.k] = 1
    return held


def evaluate(market, cand, cost_mult=1.0):
    """Daily venue returns, the trade list and the 5m-grid exposure of one candidate."""
    n_days = market.n // DAY_BARS
    names = ALTS if cand.venue == "alts" else ("BTCUSD_PERP",)
    sleeves, trades, exposure = [], [], []
    if cand.family == "rotation":
        r, pos, k = rotation(market, cand, cost_mult)
        tr = position_trades(r, pos, k)
        return daily_from_bars(r, k, n_days)[None, :], [tr], [np.repeat(pos, k)]
    for name in names:
        if cand.family == "hold":
            b = market.bars(name, "1d")
            pos = np.ones(len(b["close"]))
            pos[0] = 0
            r, pos = run_positions(name, b, pos, cost_mult)
            sleeves.append(daily_from_bars(r, 288, n_days))
            trades.append(position_trades(r, pos, 288))
            exposure.append(np.repeat(pos, 288))
            continue
        if cand.family in ("ema_trend", "ts_momentum"):
            fn = ema_trend if cand.family == "ema_trend" else ts_momentum
            r, pos, k = fn(market, name, cand, cost_mult)
            sleeves.append(daily_from_bars(r, k, n_days))
            trades.append(position_trades(r, pos, k))
            exposure.append(np.repeat(np.abs(pos), k))
            continue
        fn = breakout if cand.family == "breakout" else reversion
        sides = (1,) if cand.venue == "alts" else (1, -1)
        # COIN-M long and short rules are mutually exclusive by regime; run both and merge by time.
        parts = [fn(market, name, cand, side, cost_mult) for side in sides]
        tr = merge_non_overlapping(parts)
        sleeves.append(daily_from_trades(tr, n_days))
        trades.append(tr)
        exposure.append(exposure_from_trades(tr, market.n))
    return np.vstack(sleeves), trades, exposure


def merge_non_overlapping(parts):
    """Keep trades in time order and drop any that start while another is open."""
    k = parts[0].k
    rows = sorted((e, x, r) for p in parts for e, x, r in zip(p.entry_bar, p.exit_bar, p.ret))
    keep, busy = [], -1
    for e, x, r in rows:
        if e > busy:
            keep.append((e, x, r))
            busy = x
    if not keep:
        return Trades(np.array([], int), np.array([], int), np.array([]), k)
    e, x, r = map(np.array, zip(*keep))
    return Trades(e.astype(int), x.astype(int), r, k)


def metrics(daily, trades, exposure, market, period):
    a = (stamp(period[0]) - market.t0) // 86_400_000
    b = min((stamp(period[1]) - market.t0) // 86_400_000, daily.shape[1])
    port = daily[:, a:b].mean(0)
    equity = np.cumprod(1 + port)
    peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    years = (b - a) / 365.25
    rets = np.concatenate([t.ret[(t.entry_bar * t.k >= a * DAY_BARS) & (t.entry_bar * t.k < b * DAY_BARS)]
                           for t in trades]) if trades else np.array([])
    held = np.mean([e[a * DAY_BARS:b * DAY_BARS].mean() for e in exposure]) if exposure else 0.0
    n = len(rets)
    return {"return": float(equity[-1] - 1), "cagr": float(equity[-1] ** (1 / years) - 1),
            "max_dd": float(np.max(1 - equity / peak)), "trades": int(n),
            "mean_trade": float(rets.mean()) if n else None,
            "win_rate": float((rets > 0).mean()) if n else None,
            "t_stat": float(rets.mean() / rets.std(ddof=1) * math.sqrt(n)) if n > 2 and rets.std() > 0 else None,
            "exposure": float(held)}


def spearman(x, y):
    rx, ry = pd.Series(x).rank().to_numpy(), pd.Series(y).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def study(output=OUTPUT):
    market = Market()
    rows = []
    for cand in candidates():
        daily, trades, exposure = evaluate(market, cand)
        daily2, trades2, exposure2 = evaluate(market, cand, cost_mult=2.0)
        row = {"name": cand.name, "family": cand.family, "venue": cand.venue, "params": cand.params,
               "is_baseline": cand.family == "breakout" and cand.params == BASELINE}
        for label, period in (("dev", DEV), ("holdout", HOLDOUT)):
            row[label] = metrics(daily, trades, exposure, market, period)
            row[label + "_cost2"] = metrics(daily2, trades2, exposure2, market, period)
        rows.append(row)
        print(f"{cand.venue:5} {cand.name:70} dev {row['dev']['return']:+8.2%} ({row['dev']['trades']:5d})"
              f"  x2 {row['dev_cost2']['return']:+8.2%}", flush=True)
    summary = {"dev": DEV, "holdout": HOLDOUT, "costs": {"fee": FEE, "slip": SLIP, "tick": TICK},
               "venues": {}}
    for venue in ("alts", "coinm"):
        pool = [r for r in rows if r["venue"] == venue and r["family"] != "hold"]
        eligible = [r for r in pool if r["dev"]["trades"] >= 30 and r["dev_cost2"]["return"] > 0]
        chosen = sorted(eligible, key=lambda r: -r["dev"]["cagr"])[:3]
        summary["venues"][venue] = {
            "candidates": len(pool), "eligible": len(eligible),
            "dev_positive": sum(r["dev"]["return"] > 0 for r in pool),
            "holdout_positive": sum(r["holdout"]["return"] > 0 for r in pool),
            "rank_corr_dev_holdout": spearman([r["dev"]["cagr"] for r in pool], [r["holdout"]["cagr"] for r in pool]),
            "chosen": [r["name"] for r in chosen]}
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    return summary, rows


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    summary, _ = study()
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
