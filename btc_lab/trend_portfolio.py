"""Pre-registered test: does spreading the live 4h EMA20/80 trend rule across six coins help?

Fixed on 2026-09-26 before any result was read:
- Coins: BTC (COIN-M BTCUSD_PERP, inverse, BTC collateral) and ETH, SOL, BNB, XRP,
  DOGE (USDT-M perpetuals held against BTC collateral). Each follows the live
  rule unchanged: sign of EMA20 - EMA80 of its completed 4h closes (EMAs seeded
  over the last 200 bars, as btc_portfolio.signals), held from the next bar.
  No per-coin tuning. No stops.
- Accounting in BTC: BTC leg per unit notional 1 - B0/B1; a USDT-M leg's USD
  P&L converted at the bar's closing BTC price, (A1/A0 - 1) * B0/B1. Funding
  from the archives. Costs 0.05% fee + 3 bps per side on every change of
  BTC-notional exposure.
- Portfolios:
  S1 BTC only (weight 1, the live COIN-M rule),
  S2 equal weight 1/6 each,
  S3 equal risk: weights proportional to 1/sigma (prior 180 bars), scaled so the
     ex-ante volatility of the signed portfolio (prior 180-bar covariance)
     equals BTC's prior 180-bar volatility; gross capped at 3; weights reset
     daily, signals act every bar.
- Periods: DEV 2021-07-01..2024-07-01 (includes the 2021-23 chop) and
  HOLDOUT 2024-07-01..2026-09-01, both reported.
- Adoption of S3: higher Sharpe and no deeper max drawdown than S1 in both periods.
Survivorship: these six coins are large caps that are still listed today.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import math
import zipfile

import numpy as np
import pandas as pd

from btc_lab import aggressive_backtest as ab
from btc_lab import intraday_data as idata
from btc_lab import strategy_search as ss

OUTPUT = idata.ROOT / "btc_lab/state/trend_portfolio_20260926"
DATA = idata.ROOT / "btc_lab/state/trend_market/trend_4h.npz"
BAR4 = 14_400_000
START = (2021, 1)
LAST = (2026, 8)
ALTS = ("ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
COINS = ("BTC",) + ALTS
COST = 0.0005 + 0.0003
PERIODS = {"dev": ("2021-07-01", "2024-07-01"), "holdout": ("2024-07-01", "2026-09-01")}
LOOKBACK = 180
DAY_BARS = 6


def fetch_rows(template, months):
    with ThreadPoolExecutor(8) as pool:
        raws = list(pool.map(lambda m: idata.fetch("https://data.binance.vision/data/" + template.format(m=m)), months))
    rows, missing = [], []
    for m, raw in zip(months, raws):
        if raw is None:
            missing.append(m)
            continue
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                rows += [r for r in csv.reader(io.StringIO(z.read(name).decode())) if r and r[0][:1].isdigit()]
    return rows, missing


def build():
    months = list(idata.months(START, LAST))
    t0 = int(pd.Timestamp("2021-01-01", tz="UTC").timestamp() * 1000)
    t1 = int(pd.Timestamp("2026-09-01", tz="UTC").timestamp() * 1000)
    grid = np.arange(t0, t1, BAR4, dtype=np.int64)
    data, manifest = {"open_ms": grid}, {}

    def place(times, values):
        out = np.full(len(grid), np.nan)
        idx = (np.asarray(times, dtype=np.int64) - t0) // BAR4
        ok = (idx >= 0) & (idx < len(grid))
        out[idx[ok]] = np.asarray(values, dtype=float)[ok]
        return out

    def funding_into_bars(times, rates):
        out = np.zeros(len(grid))
        idx = (np.asarray(times, dtype=np.int64) - t0) // BAR4
        ok = (idx >= 0) & (idx < len(grid))
        np.add.at(out, idx[ok], np.asarray(rates, dtype=float)[ok])
        return out

    rows, missing = fetch_rows("futures/cm/monthly/klines/BTCUSD_PERP/4h/BTCUSD_PERP-4h-{m}.zip", months)
    data["BTC_close"] = place([int(r[0]) for r in rows], [float(r[4]) for r in rows])
    manifest["BTC_missing"] = missing
    fund = pd.read_csv(ss.FUNDING)
    data["BTC_funding"] = funding_into_bars(fund["funding_time_ms"], fund["funding_rate"])
    for s in ALTS:
        rows, missing = fetch_rows(f"futures/um/monthly/klines/{s}/4h/{s}-4h-{{m}}.zip", months)
        data[f"{s}_close"] = place([int(r[0]) for r in rows], [float(r[4]) for r in rows])
        frows, fmissing = fetch_rows(f"futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{{m}}.zip", months)
        data[f"{s}_funding"] = funding_into_bars([int(r[0]) for r in frows], [float(r[2]) for r in frows])
        manifest[s + "_missing"] = {"klines": missing, "funding": fmissing}
    DATA.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(DATA, **data)
    return manifest


def load():
    z = np.load(DATA)
    d = {k: z[k] for k in z.files}
    for c in COINS:
        d[f"{c}_close"] = pd.Series(d[f"{c}_close"]).ffill().to_numpy()
    return d


def signals(close):
    """+1/-1/0 usable during each 4h bar: window_ema at i uses closes before bar i."""
    ok = np.isfinite(close)
    fast, slow = np.full(len(close), np.nan), np.full(len(close), np.nan)
    v = close[ok]
    fast[ok], slow[ok] = ab.window_ema(v, 20), ab.window_ema(v, 80)
    return np.nan_to_num(np.sign(fast - slow))


def bar_returns(d):
    """Per-bar BTC return of one unit of BTC-notional long exposure in each coin, and funding."""
    b = d["BTC_close"]
    prev_b = np.r_[np.nan, b[:-1]]
    out, fund = {}, {}
    out["BTC"] = 1 - prev_b / b
    fund["BTC"] = d["BTC_funding"]
    for s in ALTS:
        a = d[f"{s}_close"]
        prev_a = np.r_[np.nan, a[:-1]]
        out[s] = (a / prev_a - 1) * prev_b / b
        fund[s] = d[f"{s}_funding"] * prev_b / b
    return out, fund


def weights(d, kind, sig, raw):
    n = len(d["open_ms"])
    w = {c: np.zeros(n) for c in COINS}
    if kind == "S1":
        w["BTC"][:] = 1.0
        return w
    if kind == "S2":
        for c in COINS:
            w[c][:] = 1 / len(COINS)
        return w
    logret = {c: np.diff(np.log(d[f"{c}_close"]), prepend=np.nan) for c in COINS}
    R = np.column_stack([logret[c] for c in COINS])
    current = np.zeros(len(COINS))
    for i in range(n):
        if i % DAY_BARS == 0 and i > LOOKBACK:
            window = R[i - LOOKBACK:i]
            good = np.all(np.isfinite(window), axis=1)
            if good.sum() >= LOOKBACK // 2:
                sd = window[good].std(0, ddof=1)
                base = (1 / sd) / (1 / sd).sum()
                s = np.array([sig[c][i] for c in COINS])
                cov = np.cov(window[good].T)
                port = math.sqrt(max((base * s) @ cov @ (base * s), 1e-18))
                k = min(sd[0] / port, 3 / base.sum()) if port > 0 else 0.0
                current = base * k
        for j, c in enumerate(COINS):
            w[c][i] = current[j]
    return w


def portfolio(d, kind):
    sig = {c: signals(d[f"{c}_close"]) for c in COINS}
    raw, fund = bar_returns(d)
    w = weights(d, kind, sig, raw)
    n = len(d["open_ms"])
    r = np.zeros(n)
    legs = {}
    for c in COINS:
        pos = w[c] * sig[c]                                     # both known when bar i opens
        leg = np.nan_to_num(pos * raw[c]) - np.nan_to_num(pos * fund[c])
        leg -= np.abs(np.diff(pos, prepend=0.0)) * COST
        legs[c] = leg
        r += leg
    return r, legs


def metrics(d, r, period):
    t = d["open_ms"]
    a = np.searchsorted(t, int(pd.Timestamp(period[0], tz="UTC").timestamp() * 1000))
    b = np.searchsorted(t, int(pd.Timestamp(period[1], tz="UTC").timestamp() * 1000))
    rr = r[a:b]
    daily = pd.Series(rr).groupby(np.arange(len(rr)) // DAY_BARS).apply(lambda x: np.prod(1 + x) - 1).to_numpy()
    eq = np.cumprod(1 + daily)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    years = len(daily) / 365
    w30 = np.array([eq[k + 30] / eq[k] - 1 for k in range(len(eq) - 30)])
    return {"return": float(eq[-1] - 1), "cagr": float(eq[-1] ** (1 / years) - 1),
            "vol": float(daily.std(ddof=1) * math.sqrt(365)),
            "sharpe": float(daily.mean() / daily.std(ddof=1) * math.sqrt(365)),
            "max_dd": float(np.max(1 - eq / peak)), "worst_30d": float(w30.min())}


def study(output=OUTPUT):
    d = load()
    out = {"periods": PERIODS, "portfolios": {}, "coins": {}}
    for kind in ("S1", "S2", "S3"):
        r, legs = portfolio(d, kind)
        out["portfolios"][kind] = {p: metrics(d, r, per) for p, per in PERIODS.items()}
        if kind == "S2":
            for c in COINS:
                out["coins"][c] = {p: metrics(d, legs[c] * len(COINS), per) for p, per in PERIODS.items()}
            daily = {c: pd.Series(legs[c]).groupby(np.arange(len(legs[c])) // DAY_BARS).sum() for c in COINS}
            corr = pd.DataFrame(daily).corr().to_numpy()
            out["mean_pairwise_corr_of_coin_trends"] = float(corr[np.triu_indices(len(COINS), 1)].mean())
    s1, s3 = out["portfolios"]["S1"], out["portfolios"]["S3"]
    out["adopt_S3"] = all(s3[p]["sharpe"] > s1[p]["sharpe"] and s3[p]["max_dd"] <= s1[p]["max_dd"] for p in PERIODS)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(out, indent=1))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("data", "study"))
    args = parser.parse_args(argv)
    if args.action == "data":
        print(json.dumps(build(), indent=1))
        return
    out = study()
    for kind, v in out["portfolios"].items():
        print(kind, "  ".join(f"{p}: ret {m['return']*100:+.1f}% cagr {m['cagr']*100:+.1f}% sharpe {m['sharpe']:.2f} "
                              f"maxDD {m['max_dd']*100:.1f}% worst30d {m['worst_30d']*100:+.1f}%" for p, m in v.items()))
    for c, v in out["coins"].items():
        print(f"  coin {c:8}", "  ".join(f"{p}: ret {m['return']*100:+.1f}% sharpe {m['sharpe']:.2f} maxDD {m['max_dd']*100:.1f}%"
                                      for p, m in v.items()))
    print("mean pairwise correlation of coin trend returns:", round(out["mean_pairwise_corr_of_coin_trends"], 2))
    print("ADOPT S3:", out["adopt_S3"])


if __name__ == "__main__":
    main()
