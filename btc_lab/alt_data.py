"""Hourly BTC-denominated history of every Binance spot alt, delisted ones included.

Public archives only (data.binance.vision; its S3 listing for symbol discovery).
Each base asset gets BTC-denominated OHLC from its USDT pair divided by BTCUSDT
(or its BTC pair when no USDT pair traded), USD volume summed over both pairs,
the taker-buy share, and the BTC pair's own volume, which decides the cheaper
execution route. Stablecoins, fiat, leveraged tokens and BTC/ETH wrappers are
excluded. The current book is sampled once to model spread from volume.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "btc_lab/state/alt_market"
HOUR = 3_600_000
FIRST, LAST = (2022, 1), (2026, 8)
T0 = 1640995200000                         # 2022-01-01 UTC
T1 = 1788220800000                         # 2026-09-01 UTC
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
EXCLUDED = {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "PAX", "DAI", "USDS", "USDSB", "SUSD", "UST", "USTC", "AEUR",
            "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "BVND", "PLN", "RON",
            "ARS", "JPY", "MXN", "COP", "CZK", "EURI", "XUSD", "USD1", "BFUSD", "RLUSD", "USDE",
            "BULL", "BEAR", "WBTC", "BTC", "BTCB", "BETH", "WBETH", "STETH", "BNSOL", "PAXG", "XAUT"}
FIELDS = ("open", "high", "low", "close", "volume_usd", "taker_buy_share", "btc_pair_volume_btc")


def get(url, attempts=6):
    host = urlsplit(url).netloc
    if not url.startswith("https://") or host not in {"data.binance.vision", "s3-ap-northeast-1.amazonaws.com", "api.binance.com"}:
        raise ValueError("Only public Binance market data hosts are allowed")
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": "btc-lab-alts/1"}), timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(2 + 2 * attempt)
        except OSError:
            time.sleep(2 + 2 * attempt)
    raise RuntimeError("Public download failed: " + urlsplit(url).path)


def listing(prefix):
    out, marker = [], ""
    while True:
        x = get(LISTING + "?" + urlencode({"delimiter": "/", "prefix": prefix, "marker": marker})).decode()
        items = re.findall(r"<Prefix>([^<]+)</Prefix>", x)[1:] + re.findall(r"<Key>([^<]+)</Key>", x)
        out += items
        if "<IsTruncated>true</IsTruncated>" not in x:
            return out
        marker = items[-1]


def leveraged(base, bases):
    return any(base.endswith(s) and base[:-len(s)] in bases for s in ("UP", "DOWN", "BULL", "BEAR"))


def universe():
    symbols = [p.rstrip("/").split("/")[-1] for p in listing("data/spot/monthly/klines/")]
    pairs = {q: {s[:-len(q)] for s in symbols if s.endswith(q) and s[:-len(q)].isascii() and s[:-len(q)].isalnum()}
             for q in ("USDT", "BTC")}
    bases = pairs["USDT"] | pairs["BTC"]
    keep = sorted(b for b in bases if b not in EXCLUDED and not leveraged(b, bases))
    return {q: sorted(set(keep) & pairs[q]) for q in pairs}


def months():
    y, m = FIRST
    while (y, m) <= LAST:
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def parse(raw):
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            for r in csv.reader(io.StringIO(z.read(name).decode())):
                if r and r[0][:1].isdigit():
                    t = int(r[0])
                    rows.append((t // 1000 if t > 10**14 else t, *map(float, (r[1], r[2], r[3], r[4], r[7], r[10]))))
    return rows


def symbol_series(symbol, cache):
    """open_ms-aligned hourly arrays for one pair, cached as npz."""
    path = cache / f"{symbol}.npz"
    if path.exists():
        return dict(np.load(path))
    wanted = set(months())
    files = [f for f in listing(f"data/spot/monthly/klines/{symbol}/1h/")
             if f.endswith(".zip") and f[-11:-4] in wanted]
    rows = []
    for f in files:
        raw = get("https://data.binance.vision/" + f)
        if raw:
            rows += parse(raw)
    n = (T1 - T0) // HOUR
    out = {k: np.full(n, np.nan, dtype=np.float32) for k in ("open", "high", "low", "close", "quote_volume", "taker_buy_quote")}
    if rows:
        a = np.asarray(rows, dtype=np.float64)
        idx = ((a[:, 0] - T0) // HOUR).astype(np.int64)
        ok = (idx >= 0) & (idx < n) & ((a[:, 0] - T0) % HOUR == 0)
        for j, k in enumerate(out, start=1):
            out[k][idx[ok]] = a[ok, j]
    np.savez_compressed(path, **out)
    return out


def book_sample():
    """Current relative spread and 24h quote volume of every trading USDT and BTC pair."""
    t24 = {t["symbol"]: t for t in json.loads(get("https://api.binance.com/api/v3/ticker/24hr"))}
    out = {}
    for b in json.loads(get("https://api.binance.com/api/v3/ticker/bookTicker")):
        bid, ask, t = float(b["bidPrice"]), float(b["askPrice"]), t24.get(b["symbol"])
        if bid > 0 and ask >= bid and t and float(t["quoteVolume"]) > 0:
            out[b["symbol"]] = {"spread": (ask - bid) / ((ask + bid) / 2), "quote_volume_24h": float(t["quoteVolume"])}
    return out


def build(output=OUTPUT, threads=24):
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "pairs"
    cache.mkdir(exist_ok=True)
    u = universe()
    symbols = ["BTCUSDT"] + [b + "USDT" for b in u["USDT"]] + [b + "BTC" for b in u["BTC"]]
    started = time.time()
    with ThreadPoolExecutor(threads) as pool:
        series = dict(zip(symbols, pool.map(lambda s: symbol_series(s, cache), symbols)))
    btc = series["BTCUSDT"]
    bases = sorted(set(u["USDT"]) | set(u["BTC"]))
    n = len(btc["close"])
    data = {k: np.full((len(bases), n), np.nan, dtype=np.float32) for k in FIELDS}
    for i, base in enumerate(bases):
        usd, direct = series.get(base + "USDT"), series.get(base + "BTC")
        route_usd = usd is not None and np.isfinite(usd["close"])
        for k in ("open", "high", "low", "close"):
            synthetic = usd[k] / (btc["close"] if k in ("high", "low") else btc[k]) if usd is not None else np.full(n, np.nan)
            data[k][i] = np.where(route_usd, synthetic, direct[k] if direct is not None else np.nan)
        vol = np.zeros(n)
        taker = np.zeros(n)
        if usd is not None:
            vol += np.nan_to_num(usd["quote_volume"])
            taker += np.nan_to_num(usd["taker_buy_quote"])
        if direct is not None:
            vol += np.nan_to_num(direct["quote_volume"]) * np.nan_to_num(btc["close"])
            taker += np.nan_to_num(direct["taker_buy_quote"]) * np.nan_to_num(btc["close"])
            data["btc_pair_volume_btc"][i] = np.nan_to_num(direct["quote_volume"])
        listed = np.isfinite(data["close"][i])
        data["volume_usd"][i] = np.where(listed, vol, np.nan)
        data["taker_buy_share"][i] = np.where(listed & (vol > 0), taker / np.where(vol > 0, vol, 1), np.nan)
    np.savez_compressed(output / "alts_1h.npz", bases=np.asarray(bases), open_ms=T0 + HOUR * np.arange(n, dtype=np.int64),
                        btc_usd=btc["close"], **data)
    book = book_sample()
    (output / "book_sample.json").write_text(json.dumps(book))
    manifest = {"bases": len(bases), "usdt_pairs": len(u["USDT"]), "btc_pairs": len(u["BTC"]), "hours": int(n),
                "window": ["2022-01-01", "2026-09-01"], "minutes": round((time.time() - started) / 60, 1),
                "book_sampled_ms": int(time.time() * 1000), "book_pairs": len(book)}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(json.dumps(manifest))
    return manifest


def load(path=OUTPUT / "alts_1h.npz"):
    return dict(np.load(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--threads", type=int, default=24)
    build(threads=parser.parse_args(argv).threads)


if __name__ == "__main__":
    main()
