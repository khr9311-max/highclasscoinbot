"""
백테스트 데이터: Binance COIN-M 공개 API 에서 받아 state/cache/ 에 CSV 로 캐시한다.

  - {symbol}_1h_contract.csv  : 체결가 1시간봉 (신호·진입 돌파·목표 판정)
  - {symbol}_4h_contract.csv  : 체결가 4시간봉 (존)
  - {symbol}_1h_mark.csv      : 마크가격 1시간봉 (MARK_PRICE 손절 트리거 판정)
  - {symbol}_funding.csv      : 펀딩 이력 (시각, 비율, 마크가)
  - exchange_info_{symbol}.json : 계약 사양 스냅샷 (contractSize 등)

마감된 봉만 저장한다 (서버 시각 기준). 이미 받은 구간은 다시 받지 않는다(증분).
업비트 데이터는 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ..config.settings import INTERVAL_SECONDS
from ..exchange.contract import ContractSpec, resolve_contract
from ..exchange.market_data import (FundingRecord, Kline, MarketData, closed_only, find_gaps,
                                    klines_to_bars, parse_klines)
from ..strategy.price_action import Bars

K_COLS = ("open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms", "base_volume")


def cache_dir(state_dir: str) -> Path:
    p = Path(state_dir) / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _kpath(state_dir: str, symbol: str, interval: str, kind: str) -> Path:
    return cache_dir(state_dir) / f"{symbol}_{interval}_{kind}.csv"


def save_klines(path: Path, ks: List[Kline]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(K_COLS)
        for k in ks:
            w.writerow([k.open_time_ms, repr(k.open), repr(k.high), repr(k.low), repr(k.close),
                        repr(k.volume), k.close_time_ms, repr(k.base_volume)])
    os.replace(tmp, path)


def load_klines(path: Path) -> List[Kline]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            rows.append((int(row[0]), row[1], row[2], row[3], row[4], row[5], int(row[6]), row[7]))
    return parse_klines(rows)


def save_funding(path: Path, recs: List[FundingRecord]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("funding_time_ms", "funding_rate", "mark_price"))
        for x in recs:
            w.writerow([x.funding_time_ms, repr(x.funding_rate),
                        "" if x.mark_price is None else repr(x.mark_price)])
    os.replace(tmp, path)


def load_funding(path: Path, symbol: str) -> List[FundingRecord]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            out.append(FundingRecord(symbol, int(row[0]), float(row[1]),
                                     float(row[2]) if row[2] else None))
    return out


async def download_all(md: MarketData, symbol: str, start_ms: int, state_dir: str,
                       refresh: bool = False, log: Callable[[str], None] = print,
                       pause: float = 0.25) -> Dict[str, Any]:
    """증분 다운로드. 반환: 메타 (구간·개수·공백)."""
    now_ms = md.rest.now_ms()
    meta: Dict[str, Any] = {"symbol": symbol, "downloaded_at": time.time(), "series": {}}
    xi = await md.exchange_info()
    raw = next((s for s in xi.get("symbols", []) if s.get("symbol") == symbol), None)
    spec = resolve_contract(xi, symbol)
    with open(cache_dir(state_dir) / f"exchange_info_{symbol}.json", "w", encoding="utf-8") as f:
        json.dump({"symbols": [raw], "serverTime": xi.get("serverTime")}, f, indent=1)
    for interval, kind in (("1h", "contract"), ("4h", "contract"), ("1h", "mark")):
        path = _kpath(state_dir, symbol, interval, kind)
        have = [] if refresh else load_klines(path)
        step = INTERVAL_SECONDS[interval] * 1000
        begin = have[-1].open_time_ms + step if have else start_ms
        log(f"  {symbol} {interval} {kind}: 기존 {len(have)}봉, "
            f"{time.strftime('%Y-%m-%d', time.gmtime(begin / 1000))} 부터 받음")
        new: List[Kline] = []
        cursor = begin
        while cursor < now_ms:
            end = min(cursor + 30 * 86400 * 1000 * (4 if interval == "4h" else 1) * 2 - 1, now_ms)
            chunk = await md.history(symbol, interval, cursor, end, kind=kind)
            new.extend(chunk)
            cursor = end + 1
            await asyncio.sleep(pause)
        allk = parse_klines([(k.open_time_ms, k.open, k.high, k.low, k.close, k.volume,
                              k.close_time_ms, k.base_volume) for k in have + new])
        allk = closed_only(allk, now_ms)
        save_klines(path, allk)
        gaps = find_gaps(allk, INTERVAL_SECONDS[interval])
        meta["series"][f"{interval}_{kind}"] = {
            "bars": len(allk), "first": allk[0].open_time_ms if allk else None,
            "last": allk[-1].open_time_ms if allk else None, "gaps": len(gaps),
            "gap_examples": gaps[:5]}
        log(f"    -> {len(allk)}봉 (공백 {len(gaps)}곳)")
    fpath = cache_dir(state_dir) / f"{symbol}_funding.csv"
    fhave = [] if refresh else load_funding(fpath, symbol)
    fstart = fhave[-1].funding_time_ms + 1 if fhave else start_ms
    fnew = await md.funding_history(symbol, fstart, now_ms)
    dedup = {x.funding_time_ms: x for x in fhave + fnew}
    frecs = [dedup[k] for k in sorted(dedup)]
    save_funding(fpath, frecs)
    meta["series"]["funding"] = {"records": len(frecs),
                                 "first": frecs[0].funding_time_ms if frecs else None,
                                 "last": frecs[-1].funding_time_ms if frecs else None}
    log(f"  {symbol} funding: {len(frecs)}건")
    meta["contract"] = spec.essentials()
    with open(cache_dir(state_dir) / f"{symbol}_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, default=str)
    return meta


@dataclass
class Dataset:
    symbol: str
    ltf: Bars
    htf: Bars
    mark: Optional[Bars]                  # ltf 와 같은 인덱스로 정렬 (없는 봉은 체결가로 채움)
    mark_missing: int
    funding: List[FundingRecord]
    spec: ContractSpec
    meta: Dict[str, Any] = field(default_factory=dict)


def load_dataset(state_dir: str, symbol: str) -> Dataset:
    c = cache_dir(state_dir)
    lk = load_klines(_kpath(state_dir, symbol, "1h", "contract"))
    hk = load_klines(_kpath(state_dir, symbol, "4h", "contract"))
    mk = load_klines(_kpath(state_dir, symbol, "1h", "mark"))
    if not lk or not hk:
        raise FileNotFoundError("백테스트 데이터 없음 - 먼저 다운로드하세요 (--refresh-data 또는 첫 실행)")
    with open(c / f"exchange_info_{symbol}.json", encoding="utf-8") as f:
        spec = resolve_contract(json.load(f), symbol)
    ltf = klines_to_bars(lk, 3600)
    htf = klines_to_bars(hk, 14400)
    mark_by_t = {k.open_time_ms: k for k in mk}
    rows = []
    missing = 0
    for k in lk:
        m = mark_by_t.get(k.open_time_ms)
        if m is None or m.high <= 0:
            missing += 1
            rows.append((k.open, k.high, k.low, k.close))
        else:
            rows.append((m.open, m.high, m.low, m.close))
    arr = np.asarray(rows, dtype=float)
    mark = Bars(ltf.t, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], ltf.v, 3600)
    funding = load_funding(c / f"{symbol}_funding.csv", symbol)
    meta = {}
    mp = c / f"{symbol}_meta.json"
    if mp.exists():
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    return Dataset(symbol, ltf, htf, mark, missing, funding, spec, meta)
