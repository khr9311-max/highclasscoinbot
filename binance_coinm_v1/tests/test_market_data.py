"""4단계: 시장 데이터 - 마감 봉만 사용, 가격 종류 분리, 과거 데이터 페이지네이션."""

import pytest

from binance_coinm_v1.exchange import BinanceRestClient
from binance_coinm_v1.exchange.market_data import (MarketData, closed_only, find_gaps,
                                                   klines_to_bars, parse_klines)

from .conftest import run
from .fakes import FakeTransport, no_sleep

H = 3_600_000


def kline_row(t_ms, o=100.0, h=101.0, l=99.0, c=100.5, v=10):
    return [t_ms, str(o), str(h), str(l), str(c), str(v), t_ms + H - 1, "0.1", 5, "1", "0.01", "0"]


def md(tr, now_ms=10 * H + 5):
    rest = BinanceRestClient("https://dapi.binance.com", transport=tr,
                             clock=lambda: now_ms / 1000.0, sleep=no_sleep)
    return MarketData(rest)


def test_parse_and_closed_only_excludes_forming_bar():
    rows = [kline_row(t * H) for t in range(8, 11)]         # 8h,9h,10h 시작 봉
    ks = parse_klines(rows)
    now = 10 * H + 30_000                                   # 10시 봉은 아직 형성 중
    closed = closed_only(ks, now)
    assert [k.open_time_ms for k in closed] == [8 * H, 9 * H]
    # 정확히 마감 시각(closeTime+1ms)이면 마감으로 본다
    assert closed_only(ks, 11 * H)[-1].open_time_ms == 10 * H


def test_closed_bars_uses_server_time_not_local():
    rows = [kline_row(t * H) for t in range(0, 11)]
    tr = FakeTransport().add("GET", "/dapi/v1/klines", lambda p: rows)
    m = md(tr, now_ms=10 * H + 5)
    bars = run(m.closed_bars("BTCUSD_PERP", "1h", 5))
    assert len(bars) == 5
    assert bars.t[-1] == 9 * 3600                           # 10시 봉(형성 중) 제외
    assert bars.period == 3600
    assert tr.calls[0]["params"]["symbol"] == "BTCUSD_PERP"


def test_price_types_use_separate_endpoints():
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/markPriceKlines", lambda p: [kline_row(0, 1, 2, 0.5, 1.5, 0)])
    tr.add("GET", "/dapi/v1/indexPriceKlines", lambda p: [kline_row(0, 1, 2, 0.5, 1.5, 0)])
    tr.add("GET", "/dapi/v1/premiumIndex", lambda p: [{
        "symbol": "BTCUSD_PERP", "pair": "BTCUSD", "markPrice": "83528.8", "indexPrice": "83573.6",
        "estimatedSettlePrice": "83897.2", "lastFundingRate": "0.00003369",
        "interestRate": "0.0001", "nextFundingTime": 1790265600000, "time": 1790239751001}])
    m = md(tr)
    run(m.klines("BTCUSD_PERP", "1h", kind="mark"))
    run(m.klines("BTCUSD_PERP", "1h", kind="index"))
    assert tr.calls[0]["params"]["symbol"] == "BTCUSD_PERP"
    assert tr.calls[1]["params"]["pair"] == "BTCUSD"          # 지수봉은 pair 기준
    pi = run(m.premium_index("BTCUSD_PERP"))
    assert pi.mark_price == pytest.approx(83528.8) and pi.index_price == pytest.approx(83573.6)
    assert pi.last_funding_rate == pytest.approx(0.00003369)


def test_history_paginates_within_200_day_window():
    total = 4000

    def handler(p):
        s, e, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
        assert e - s < 200 * 86400 * 1000                    # COIN-M 200일 제한
        return [kline_row(t) for t in range(s, min(e, total * H) + 1, H)][:lim]

    tr = FakeTransport().add("GET", "/dapi/v1/klines", handler)
    ks = run(md(tr).history("BTCUSD_PERP", "1h", 0, (total - 1) * H))
    assert len(ks) == total and find_gaps(ks, 3600) == []
    assert all(int(c["params"]["limit"]) <= 1500 for c in tr.calls)


def test_history_4h_window_capped_at_200_days():
    def handler(p):
        s, e = int(p["startTime"]), int(p["endTime"])
        assert e - s < 200 * 86400 * 1000
        return [kline_row(t) for t in range(s, e + 1, 4 * H)][:int(p["limit"])]

    tr = FakeTransport().add("GET", "/dapi/v1/klines", handler)
    ks = run(md(tr).history("BTCUSD_PERP", "4h", 0, 3000 * 4 * H))
    assert len(ks) == 3001
    assert int(tr.calls[0]["params"]["limit"]) == 1200        # 200일 = 4시간봉 1200개


def test_funding_history_pagination_and_missing_mark():
    recs = [{"symbol": "BTCUSD_PERP", "fundingTime": i * 8 * H, "fundingRate": "0.0001",
             "markPrice": "" if i < 3 else "84000", "rateType": "Regular"} for i in range(2500)]

    def handler(p):
        s = int(p["startTime"])
        return [r for r in recs if r["fundingTime"] >= s][:int(p["limit"])]

    tr = FakeTransport().add("GET", "/dapi/v1/fundingRate", handler)
    out = run(md(tr, now_ms=2500 * 8 * H).funding_history("BTCUSD_PERP", 0))
    assert len(out) == 2500 and out[0].mark_price is None and out[5].mark_price == 84000.0
    assert len(tr.calls) == 3


def test_gap_detection_and_bars_conversion():
    ks = parse_klines([kline_row(0), kline_row(H), kline_row(3 * H)])
    assert find_gaps(ks, 3600) == [(H, 3 * H)]
    b = klines_to_bars(ks, 3600)
    assert list(b.t) == [0.0, 3600.0, 10800.0] and b.close_time(0) == 3600.0
