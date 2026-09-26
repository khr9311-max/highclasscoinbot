"""Public 5-minute order-flow and positioning history for the regime-switch study.

Adds what the OHLC cache in intraday_data lacks, on one 5-minute grid:
- taker buy volume of BTCUSD_PERP (COIN-M, contracts) and BTCUSDT (USDT-M, BTC)
- BTCUSD_PERP premium index (close of each 5m bar)
- BTCUSDT "metrics": open interest, top-trader account and position L/S,
  global account L/S, taker buy/sell ratio

Positioning comes from USDT-M because the COIN-M statistics endpoints were
observed frozen for days on 2026-09-24..26 (open interest 25,758,909
contracts, top-trader position L/S 2.4295). Monthly archives come from
data.binance.vision; the unfinished month is filled from public REST klines.
No credentials are used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
from urllib.parse import urlencode
import zipfile

import numpy as np

from btc_lab import intraday_data as idata

OUTPUT = idata.ROOT / "btc_lab/state/flow_market"
BAR = idata.BAR
START_MS = 1625097600000                 # 2021-07-01 UTC
END_MS = idata.REST_END                  # 2026-09-25 UTC
FIRST_MONTH = (2021, 7)
METRIC_FIELDS = ("oi", "oi_value", "top_account_ls", "top_position_ls", "global_ls", "taker_ratio")
KLINES = {
    # name: (archive template, rest host, rest path, symbol, {field: column})
    "cm": ("futures/cm/monthly/klines/BTCUSD_PERP/5m/BTCUSD_PERP-5m-{m}.zip",
           "dapi.binance.com", "/dapi/v1/klines", "BTCUSD_PERP", {"volume": 5, "taker_buy": 9}),
    "um": ("futures/um/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-{m}.zip",
           "fapi.binance.com", "/fapi/v1/klines", "BTCUSDT", {"volume": 5, "taker_buy": 9}),
    "premium": ("futures/cm/monthly/premiumIndexKlines/BTCUSD_PERP/5m/BTCUSD_PERP-5m-{m}.zip",
                "dapi.binance.com", "/dapi/v1/premiumIndexKlines", "BTCUSD_PERP", {"index": 4}),
}
METRICS = "futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip"


@dataclass(frozen=True)
class Window:
    """Grid [start_ms, end_ms), monthly kline archives first..last, optional REST from rest_start."""
    start_ms: int = START_MS
    end_ms: int = END_MS
    first_month: tuple = FIRST_MONTH
    last_month: tuple = idata.LAST_ARCHIVE_MONTH
    rest_start: int | None = idata.REST_START


DEFAULT = Window()


def months(w=DEFAULT):
    return idata.months(w.first_month, w.last_month)


def days(w=DEFAULT):
    day = datetime.fromtimestamp(w.start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp(w.end_ms / 1000, timezone.utc).date() - timedelta(days=1)
    while day <= last:
        yield day.isoformat()
        day += timedelta(days=1)


def csv_rows(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            for row in csv.reader(io.StringIO(archive.read(name).decode())):
                if row and row[0][:1].isdigit():
                    yield row


def rest_klines(host, path, symbol, start=idata.REST_START, end=END_MS):
    rows = []
    while start < end:
        query = {"symbol": symbol, "interval": "5m", "startTime": start, "endTime": end - 1, "limit": 1500}
        chunk = json.loads(idata.fetch(f"https://{host}{path}?" + urlencode(query)))
        if not chunk:
            break
        rows += [r for r in chunk if int(r[6]) < end]
        start = int(chunk[-1][0]) + BAR
    return rows


def grid(w=DEFAULT):
    return w.start_ms + BAR * np.arange((w.end_ms - w.start_ms) // BAR, dtype=np.int64)


def place(opens, times, values):
    """Put values on the grid by open time; missing bars stay NaN."""
    out = np.full(len(opens), np.nan)
    idx = (np.asarray(times, dtype=np.int64) - opens[0]) // BAR
    ok = (idx >= 0) & (idx < len(opens))
    out[idx[ok]] = np.asarray(values, dtype=float)[ok]
    return out


def build_klines(name, opens, w=DEFAULT):
    template, host, path, symbol, cols = KLINES[name]
    names = list(months(w))
    with ThreadPoolExecutor(6) as pool:
        archives = list(pool.map(lambda m: idata.fetch("https://data.binance.vision/data/" + template.format(m=m)),
                                 names))
    rows, missing = [], []
    for month, raw in zip(names, archives):
        if raw is None:
            missing.append(month)
        else:
            rows += list(csv_rows(raw))
    if w.rest_start is not None:
        rows += rest_klines(host, path, symbol, w.rest_start, w.end_ms)
    table = {int(r[0]): r for r in rows}
    times = np.array(sorted(table), dtype=np.int64)
    out = {f"{name}_{field}": place(opens, times, [float(table[t][c]) for t in times])
           for field, c in cols.items()}
    return out, missing


def metric_time(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def build_metrics(opens, w=DEFAULT):
    """USDT-M metrics keyed by create_time, snapped down to the 5m grid."""
    names = list(days(w))
    with ThreadPoolExecutor(16) as pool:
        archives = list(pool.map(lambda d: idata.fetch("https://data.binance.vision/data/" + METRICS.format(d=d)),
                                 names))
    table, missing = {}, []
    for day, raw in zip(names, archives):
        if raw is None:
            missing.append(day)
            continue
        for r in csv_rows(raw):
            t = metric_time(r[0]) // BAR * BAR
            try:
                table[t] = [float(x) if x else np.nan for x in r[2:8]]
            except ValueError:
                continue
    times = np.array(sorted(table), dtype=np.int64)
    values = np.array([table[t] for t in times]) if len(times) else np.empty((0, 6))
    out = {f"um_{field}": place(opens, times, values[:, j]) for j, field in enumerate(METRIC_FIELDS)}
    return out, missing


def build_all(w=DEFAULT, output=OUTPUT):
    opens = grid(w)
    data, manifest = {"open_ms": opens}, {"start": w.start_ms, "end": w.end_ms, "bars": len(opens)}
    for name in KLINES:
        part, missing = build_klines(name, opens, w)
        data.update(part)
        manifest[name + "_missing_months"] = missing
        print(name, {k: int(np.isnan(v).sum()) for k, v in part.items()}, "missing months", missing, flush=True)
    part, missing = build_metrics(opens, w)
    data.update(part)
    manifest["metrics_missing_days"] = missing
    manifest["nan_counts"] = {k: int(np.isnan(v).sum()) for k, v in data.items() if k != "open_ms"}
    print("metrics", manifest["nan_counts"], "missing days", len(missing), flush=True)
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / "flow_5m.npz", **data)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    build_all()


if __name__ == "__main__":
    main()
