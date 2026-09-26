"""Non-Binance public history on the flow_data 5m grid, from 2022-12-01.

- Coinbase Exchange BTC-USD 5m candles (US spot demand; premium vs Binance).
- Upbit KRW-BTC and KRW-USDT 5m candles (Korean retail demand; kimchi premium).
- Deribit BTC DVOL hourly index (implied volatility from options).
A 5m candle starting at T is placed at the bar opening at T (complete at its
close, as with Binance klines). An hourly DVOL candle starting at T is placed
at the 5m bar that closes at T+1h and forward-filled. Public endpoints only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import numpy as np

from btc_lab import flow_data
from btc_lab import intraday_data as idata

OUTPUT = idata.ROOT / "btc_lab/state/external_market"
BAR = idata.BAR
START_MS = int(datetime(2022, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = flow_data.END_MS
ALLOWED = {"api.exchange.coinbase.com", "api.upbit.com", "www.deribit.com"}
_pace = threading.Lock()


def get(url, min_interval=0.12, attempts=8):
    if urlsplit(url).netloc not in ALLOWED or not url.startswith("https://"):
        raise ValueError("Host not allowed: " + urlsplit(url).netloc)
    for attempt in range(attempts):
        with _pace:                                          # stay under public rate limits
            time.sleep(min_interval)
        try:
            with urlopen(Request(url, headers={"User-Agent": "btc-lab-external/1", "Accept": "application/json"}),
                         timeout=30) as r:
                return json.loads(r.read())
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except OSError:
            pass
        time.sleep(2 + 2 * attempt)
    raise RuntimeError("Download failed: " + urlsplit(url).path)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def coinbase(opens):
    step = 300 * BAR
    starts = list(range(START_MS, END_MS, step))

    def one(s):
        q = {"granularity": 300, "start": iso(s), "end": iso(min(s + step, END_MS) - 1000)}
        return get("https://api.exchange.coinbase.com/products/BTC-USD/candles?" + urlencode(q))
    with ThreadPoolExecutor(4) as pool:
        chunks = list(pool.map(one, starts))
    rows = {int(r[0]) * 1000: r for c in chunks for r in c}
    t = np.array(sorted(rows), dtype=np.int64)
    return {"cb_close": flow_data.place(opens, t, [float(rows[k][4]) for k in t]),
            "cb_volume": flow_data.place(opens, t, [float(rows[k][5]) for k in t])}


def upbit(opens, market):
    rows, to = {}, END_MS
    while to > START_MS:
        chunk = get("https://api.upbit.com/v1/candles/minutes/5?" + urlencode({"market": market, "to": iso(to), "count": 200}))
        if not chunk:
            break
        for r in chunk:
            t = int(datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
            rows[t] = r
        to = min(rows)
    t = np.array(sorted(k for k in rows if k >= START_MS), dtype=np.int64)
    key = market.split("-")[1].lower()
    return {f"up_{key}_close": flow_data.place(opens, t, [float(rows[k]["trade_price"]) for k in t]),
            f"up_{key}_volume": flow_data.place(opens, t, [float(rows[k]["candle_acc_trade_volume"]) for k in t])}


def dvol(opens):
    rows, end = {}, END_MS
    while end > START_MS:
        q = {"currency": "BTC", "start_timestamp": START_MS, "end_timestamp": end, "resolution": 3600}
        res = get("https://www.deribit.com/api/v2/public/get_volatility_index_data?" + urlencode(q))["result"]
        data = res.get("data") or []
        if not data:
            break
        for r in data:
            rows[int(r[0])] = float(r[4])
        cont = res.get("continuation")
        if not cont or cont >= end:
            break
        end = int(cont)
    t = np.array(sorted(rows), dtype=np.int64)
    known = t + 3_600_000 - BAR                              # the 5m bar that closes when the hour completes
    return {"dvol": flow_data.place(opens, known, [rows[k] for k in t])}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    opens = flow_data.grid()
    data = {"open_ms": opens}
    for name, fn in (("coinbase", coinbase), ("upbit_btc", lambda o: upbit(o, "KRW-BTC")),
                     ("upbit_usdt", lambda o: upbit(o, "KRW-USDT")), ("dvol", dvol)):
        part = fn(opens)
        data.update(part)
        print(name, {k: int(np.isfinite(v).sum()) for k, v in part.items()}, flush=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUTPUT / "external_5m.npz", **data)


if __name__ == "__main__":
    main()
