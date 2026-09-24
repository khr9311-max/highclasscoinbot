"""Reproducible, public-only spot history for the fixed small-capital study."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
HOUR_MS = 3_600_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Unexpected public API redirect')


def public_get(path, params=None):
    if path not in ('/api/v3/klines', '/api/v3/time'):
        raise ValueError('Only public time and candles are allowed')
    params = params or {}
    if not set(params) <= {'symbol', 'interval', 'startTime', 'endTime', 'limit'}:
        raise ValueError('Unexpected public query parameter')
    if params and (params.get('symbol') != 'BTCUSDT' or params.get('interval') not in ('1h', '1d')):
        raise ValueError('Only the frozen BTCUSDT 1h/1d study is supported')
    request = Request('https://api.binance.com'+path+('?' + urlencode(params) if params else ''),
                      method='GET', headers={'User-Agent': 'btc-lab-small-capital/1'})
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Public candle response too large')
    return json.loads(raw)


def validate_chunk(rows, start, end, *, period=HOUR_MS, allow_gaps=False, allow_partial=False):
    if not isinstance(rows, list):
        raise ValueError('Candle list missing')
    timestamps = [int(r[0]) for r in rows]
    expected = set(range(start, end, period))
    if (timestamps != sorted(set(timestamps)) or not set(timestamps) <= expected
            or (not allow_gaps and set(timestamps) != expected)):
        raise ValueError(f'Missing, duplicated or reordered spot candles in {start}:{end}; no forward filling')
    for row in rows:
        full_close = int(row[0]) + period - 1
        valid_close = int(row[0]) <= int(row[6]) <= full_close if allow_partial else int(row[6]) == full_close
        if not valid_close:
            raise ValueError(f'Invalid candle close timestamp: open={row[0]}, close={row[6]}, period={period}')
        o,h,l,c = map(float, row[1:5])
        if not all(math.isfinite(x) and x > 0 for x in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
            raise ValueError('Invalid spot OHLC')
    return rows


def download(output, reference):
    output.mkdir(parents=True, exist_ok=True)
    with reference.open(encoding='utf-8') as stream:
        reference_rows = list(csv.DictReader(stream))
    start = int(reference_rows[0]['open_time_ms'])
    end = int(reference_rows[-1]['open_time_ms']) + HOUR_MS
    if int(public_get('/api/v3/time')['serverTime']) < end:
        raise ValueError('Requested history contains a still-open candle')
    chunks_dir = output/'spot_chunks'
    chunks_dir.mkdir(exist_ok=True)

    def chunk(window):
        interval,period,left,right = window
        path = chunks_dir/f'{interval}_{left}_{right}.json'
        if path.exists():
            return validate_chunk(json.loads(path.read_text(encoding='utf-8')), left, right,
                                  period=period, allow_gaps=True, allow_partial=True)
        for attempt in range(3):
            try:
                rows = public_get('/api/v3/klines', {'symbol':'BTCUSDT','interval':interval,
                    'startTime':left,'endTime':right-1,'limit':1000})
                validate_chunk(rows, left, right, period=period, allow_gaps=True, allow_partial=True)
                with path.open('x', encoding='utf-8') as stream:
                    json.dump(rows, stream, separators=(',', ':'), allow_nan=False)
                return rows
            except (TimeoutError, OSError):
                if attempt == 2:
                    raise
                time.sleep(.5 * (attempt+1))
        raise AssertionError('Unreachable')

    datasets = {}
    for interval,period in (('1h', HOUR_MS), ('1d', 24*HOUR_MS)):
        first,finish = start//period*period, end//period*period
        windows = [(interval,period,t,min(t+1000*period,finish)) for t in range(first,finish,1000*period)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = [row for part in pool.map(chunk, windows) for row in part]
        validate_chunk(rows, first, finish, period=period, allow_gaps=True, allow_partial=True)
        api_timestamps = {int(row[0]) for row in rows}
        partial = [{'open_time_ms':int(row[0]), 'actual_close_time_ms':int(row[6])}
                   for row in rows if int(row[6]) != int(row[0])+period-1]
        rows = [row for row in rows if int(row[6]) == int(row[0])+period-1]
        csv_path = output/f'BTCUSDT_{interval}_spot.csv'
        lines = ['open_time_ms,open,high,low,close\n']
        lines.extend(','.join(str(x) for x in row[:5])+'\n' for row in rows)
        data = ''.join(lines).encode('utf-8')
        if csv_path.exists():
            if csv_path.read_bytes() != data:
                raise ValueError('Prior study history differs; choose a new output directory')
        else:
            with csv_path.open('xb') as stream:
                stream.write(data)
        timestamps = {int(row[0]) for row in rows}
        missing = sorted(set(range(first, finish, period))-timestamps)
        datasets[interval] = {'file':csv_path.name, 'start_ms':first, 'end_exclusive_ms':finish,
                             'candles':len(rows), 'data_sha256':hashlib.sha256(data).hexdigest(),
                             'missing_open_times_ms':missing, 'missing_count':len(missing),
                             'api_missing_open_times_ms':sorted(set(range(first, finish, period))-api_timestamps),
                             'excluded_partial_candles':partial,
                             'missing_data_action':'No invented bars or fills; daily signals use actual separate daily candles'}
    manifest = {'fetched_utc':datetime.now(timezone.utc).isoformat(), 'symbol':'BTCUSDT',
                'source':'https://api.binance.com/api/v3/klines', 'public_get_only':True,
                'start_ms':start, 'end_exclusive_ms':end, 'datasets':datasets,
                'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'reference_window_sha256':hashlib.sha256(reference.read_bytes()).hexdigest(),
                'limitations':['Spot USDT price; not COIN-M USD or futures mark history.',
                               'No order book, historical trading filters or stablecoin depeg model included.']}
    manifest_path = output/'spot_data_manifest.json'
    if not manifest_path.exists():
        with manifest_path.open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, indent=2, allow_nan=False)
    print(json.dumps(manifest))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'btc_lab/state/small_capital_market')
    parser.add_argument('--reference', type=Path, default=ROOT/'binance_coinm_v1/state/cache/BTCUSD_PERP_1h_contract.csv')
    args = parser.parse_args()
    download(args.output, args.reference)
