"""Public 5-minute history for the intraday strategy search.

Monthly archives come from data.binance.vision; the unfinished month is filled
from the public klines REST endpoint. Spot archives from 2025 use microsecond
timestamps and are normalised to milliseconds. No credentials are used.
Output: one .npz per series with open_ms, open, high, low, close, volume.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "btc_lab/state/intraday_market"
BAR = 300_000
FIRST_MONTH = (2021, 1)
LAST_ARCHIVE_MONTH = (2026, 8)
REST_START = 1788220800000   # 2026-09-01 UTC
REST_END = 1790294400000     # 2026-09-25 UTC
SERIES = {
    # name: (archive path template, rest host, rest path, rest symbol parameter)
    "ETHBTC": ("spot/monthly/klines/ETHBTC/5m/ETHBTC-5m-{m}.zip", "api.binance.com", "/api/v3/klines", "ETHBTC"),
    "BNBBTC": ("spot/monthly/klines/BNBBTC/5m/BNBBTC-5m-{m}.zip", "api.binance.com", "/api/v3/klines", "BNBBTC"),
    "SOLBTC": ("spot/monthly/klines/SOLBTC/5m/SOLBTC-5m-{m}.zip", "api.binance.com", "/api/v3/klines", "SOLBTC"),
    "XRPBTC": ("spot/monthly/klines/XRPBTC/5m/XRPBTC-5m-{m}.zip", "api.binance.com", "/api/v3/klines", "XRPBTC"),
    "BTCUSD_PERP": ("futures/cm/monthly/klines/BTCUSD_PERP/5m/BTCUSD_PERP-5m-{m}.zip",
                    "dapi.binance.com", "/dapi/v1/klines", "BTCUSD_PERP"),
    "BTCUSD_PERP_mark": ("futures/cm/monthly/markPriceKlines/BTCUSD_PERP/5m/BTCUSD_PERP-5m-{m}.zip",
                         "dapi.binance.com", "/dapi/v1/markPriceKlines", "BTCUSD_PERP"),
}
ALLOWED = {"data.binance.vision", "api.binance.com", "dapi.binance.com"}


def fetch(url, attempts=6):
    if urlsplit(url).netloc not in ALLOWED or not url.startswith("https://"):
        raise ValueError("Only public Binance market data hosts are allowed")
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": "btc-lab-intraday/1"}), timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(2 + 2 * attempt)
        except OSError:
            time.sleep(2 + 2 * attempt)
    raise RuntimeError("Public download failed: " + urlsplit(url).path)


def months():
    year, month = FIRST_MONTH
    while (year, month) <= LAST_ARCHIVE_MONTH:
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def parse_archive(raw):
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode()
            for row in csv.reader(io.StringIO(text)):
                if not row or not row[0].isdigit():
                    continue  # header line in newer archives
                rows.append(row[:6])
    return rows


def rest_rows(host, path, symbol):
    rows, start = [], REST_START
    limit = 1500 if host == "dapi.binance.com" else 1000
    while start < REST_END:
        query = {"symbol": symbol, "interval": "5m", "startTime": start,
                 "endTime": REST_END - 1, "limit": limit}
        if path == "/dapi/v1/markPriceKlines":
            query.pop("symbol")
            query["symbol"] = symbol
        chunk = json.loads(fetch(f"https://{host}{path}?" + urlencode(query)))
        if not chunk:
            break
        rows += [r[:6] for r in chunk if int(r[6]) < REST_END]
        start = int(chunk[-1][0]) + BAR
    return rows


def build(name, output=OUTPUT):
    template, host, path, symbol = SERIES[name]
    with ThreadPoolExecutor(6) as pool:
        archives = list(pool.map(lambda m: fetch("https://data.binance.vision/data/" + template.format(m=m)), months()))
    rows = []
    missing = []
    for month, raw in zip(months(), archives):
        if raw is None:
            missing.append(month)
            continue
        rows += parse_archive(raw)
    rows += rest_rows(host, path, symbol)
    table = {}
    for r in rows:
        t = int(r[0])
        if t > 10**14:  # microsecond spot archives since 2025
            t //= 1000
        table[t] = [float(x) for x in r[1:6]]
    opens = np.array(sorted(table), dtype=np.int64)
    values = np.array([table[t] for t in opens], dtype=np.float64)
    if np.any(opens % BAR) or np.any(np.diff(opens) <= 0):
        raise ValueError(name + ": misaligned or duplicated 5m candles")
    o, h, low, c = values[:, 0], values[:, 1], values[:, 2], values[:, 3]
    if np.any(values[:, :4] <= 0) or np.any(low > np.minimum(o, c)) or np.any(h < np.maximum(o, c)):
        raise ValueError(name + ": invalid OHLC")
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / f"{name}_5m.npz", open_ms=opens, open=o, high=h, low=low, close=c,
                        volume=values[:, 4])
    gaps = int(np.sum(np.diff(opens) != BAR))
    return {"series": name, "bars": len(opens), "first": int(opens[0]), "last": int(opens[-1]),
            "gaps": gaps, "missing_archive_months": missing}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--series", nargs="*", default=list(SERIES))
    args = parser.parse_args(argv)
    manifest = [build(name) for name in args.series]
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    for row in manifest:
        print(row, flush=True)


if __name__ == "__main__":
    main()
