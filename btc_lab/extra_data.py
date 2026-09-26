"""Order-book depth and cross-coin 5m history for the model search, on the flow_data grid.

- bookDepth (data.binance.vision daily, ~30 s snapshots): cumulative bid/ask
  quantity within 0.2%, 1% and 5% of the price, for USDT-M BTCUSDT and COIN-M
  BTCUSD_PERP from 2023-01-01. Each 5m bar keeps its last snapshot taken before
  the bar close, so a value at index i is known at the close of bar i.
- USDT-M 5m klines (monthly archives) for ETH, SOL, BNB, XRP and DOGE: close,
  volume and taker buy volume.
Public data only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import io
import json
import zipfile

import numpy as np
import pandas as pd

from btc_lab import flow_data
from btc_lab import intraday_data as idata

OUTPUT = idata.ROOT / "btc_lab/state/extra_market"
BAR = idata.BAR
DEPTH_START = date(2023, 1, 1)
DEPTH_END = date(2026, 9, 24)
DEPTH = {"um": "futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-{d}.zip",
         "cm": "futures/cm/daily/bookDepth/BTCUSD_PERP/BTCUSD_PERP-bookDepth-{d}.zip"}
COINS = ("ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
COIN_TEMPLATE = "futures/um/monthly/klines/{s}/5m/{s}-5m-{m}.zip"
BANDS = (0.2, 1.0, 5.0)


def depth_day(raw, start_ms):
    """(bar index, imbalance per band, total within 1%) from one daily bookDepth archive."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        d = pd.read_csv(z.open(z.namelist()[0]))
    d["ms"] = pd.to_datetime(d["timestamp"], utc=True).astype("int64") // 1_000_000
    wide = d.pivot_table(index="ms", columns="percentage", values="depth", aggfunc="last")
    out = {}
    for b in BANDS:
        bid, ask = wide.get(-b), wide.get(b)
        if bid is None or ask is None:
            out[f"imb_{b:g}"] = pd.Series(np.nan, index=wide.index)
        else:
            out[f"imb_{b:g}"] = (bid - ask) / (bid + ask)
    out["total_1"] = wide.get(-1.0, np.nan) + wide.get(1.0, np.nan)
    frame = pd.DataFrame(out, index=wide.index)
    frame["bar"] = (frame.index.to_numpy() - start_ms) // BAR
    return frame.groupby("bar").last()


def build_depth(opens, venue):
    days = [DEPTH_START + timedelta(days=k) for k in range((DEPTH_END - DEPTH_START).days + 1)]
    with ThreadPoolExecutor(16) as pool:
        raws = list(pool.map(lambda d: idata.fetch("https://data.binance.vision/data/" + DEPTH[venue].format(d=d)), days))
    arrays = {f"{venue}_imb_{b:g}": np.full(len(opens), np.nan) for b in BANDS}
    arrays[f"{venue}_depth_1"] = np.full(len(opens), np.nan)
    missing = []
    for day, raw in zip(days, raws):
        if raw is None:
            missing.append(day.isoformat())
            continue
        f = depth_day(raw, int(opens[0]))
        idx = f.index.to_numpy()
        ok = (idx >= 0) & (idx < len(opens))
        for b in BANDS:
            arrays[f"{venue}_imb_{b:g}"][idx[ok]] = f[f"imb_{b:g}"].to_numpy()[ok]
        arrays[f"{venue}_depth_1"][idx[ok]] = f["total_1"].to_numpy()[ok]
    return arrays, missing


def build_coins(opens):
    months = list(idata.months((2022, 12), (2026, 8)))
    arrays, missing = {}, {}
    for s in COINS:
        with ThreadPoolExecutor(8) as pool:
            raws = list(pool.map(lambda m: idata.fetch("https://data.binance.vision/data/" + COIN_TEMPLATE.format(s=s, m=m)), months))
        rows = []
        missing[s] = [m for m, r in zip(months, raws) if r is None]
        for r in raws:
            if r is not None:
                rows += list(flow_data.csv_rows(r))
        table = {int(r[0]): r for r in rows}
        times = np.array(sorted(table), dtype=np.int64)
        for field, col in (("close", 4), ("volume", 5), ("taker_buy", 9)):
            arrays[f"{s}_{field}"] = flow_data.place(opens, times, [float(table[t][col]) for t in times])
    return arrays, missing


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    opens = flow_data.grid()
    data, manifest = {"open_ms": opens}, {}
    for venue in DEPTH:
        part, missing = build_depth(opens, venue)
        data.update(part)
        manifest[f"{venue}_depth_missing_days"] = missing
        print(venue, "depth bars", {k: int(np.isfinite(v).sum()) for k, v in part.items()}, "missing days", len(missing), flush=True)
    part, missing = build_coins(opens)
    data.update(part)
    manifest["coin_missing_months"] = missing
    print("coins", {k: int(np.isfinite(v).sum()) for k, v in part.items() if k.endswith("_close")}, flush=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUTPUT / "extra_5m.npz", **data)
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
