import asyncio
from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace
import time

import pytest

from btc_portfolio.intraday import indicators, exit_reason
from btc_portfolio.signals import closed
from btc_portfolio.spot_gateway import SpotGateway, GatewayError
from btc_portfolio.venues import CoinTransport


def market(direction=1):
    now = time.time_ns()//1_000_000
    def rows(period, swing):
        end = now//period*period
        result = []
        for i in range(120):
            price = D(100)+direction*(D(".05")*i+(D(".3") if i % 2 == 0 else -D(".3"))) if swing else D(100)+direction*D(".1")*i
            start = end-(120-i)*period
            result.append([start,str(price),str(price+D(".15")),str(price-D(".15")),str(price),
                           "150" if i == 119 else "100",start+period-1])
        return result
    bars = rows(300_000, True)
    quote = max(D(r[2]) for r in bars[-12:])+D(".03") if direction > 0 else min(D(r[3]) for r in bars[-12:])-D(".04")
    return {"server_time_ms":now,"received_at_ms":now,"klines":bars,"trend_klines":rows(3_600_000, False),
            "bid":str(quote),"ask":str(quote+D(".001"))}


@pytest.mark.parametrize("direction", [1,-1])
def test_current_quote_can_trigger_between_candle_closes(direction):
    m = market(direction)
    waiting = deepcopy(m)
    waiting["bid"] = waiting["ask"] = waiting["klines"][-1][4]
    before = indicators(waiting,D(".0005"))
    after = indicators(m,D(".0005"))
    assert before["bar"] == after["bar"]
    assert before["direction"] == 0
    assert after["direction"] == direction


def test_open_candle_cannot_change_indicators():
    m = market()
    expected = indicators(m,D(".0005"))
    start = m["server_time_ms"]//300_000*300_000
    m["klines"].append([start,"999","1000","1","999","99999",start+299999])
    assert indicators(m,D(".0005")) == expected


def test_cost_spread_and_staleness_block_entry():
    m = market()
    expensive = indicators(m,D(".02"))
    assert expensive["direction"] == 0 and "volatility_too_small_for_cost" in expensive["reasons"]
    m["ask"] = str(D(m["bid"])*D("1.002"))
    wide = indicators(m,D(".0005"))
    assert wide["direction"] == 0 and "spread_above_10bps" in wide["reasons"]
    m["received_at_ms"] -= 16000
    with pytest.raises(ValueError,match="stale"):
        indicators(m,D(".0005"))


@pytest.mark.parametrize("rows", [None, {}, [None], [[None,1,2,1,2,0,None]]])
def test_malformed_candles_fail_with_controlled_error(rows):
    with pytest.raises(ValueError,match="Malformed candle"):
        closed(rows,600000,300000,1)


@pytest.mark.parametrize("signed", [1,-1])
def test_intraday_exit_levels_and_time(signed):
    entry = {"price":"100","stop_fraction":".01","entered_at_ms":1}
    sig = {"exit_long":False,"exit_short":False}
    assert exit_reason(entry,100+signed*2,signed,1000,sig,14400) == "take_profit_2r"
    assert exit_reason(entry,100-signed,signed,1000,sig,14400) == "stop_loss"
    assert exit_reason(entry,100,signed,14400001,sig,14400) == "max_hold_time"
    assert exit_reason(entry,100,signed,1000,sig,14400) is None


def test_intraday_gateway_only_allows_explicit_timeframes():
    g = SpotGateway("ETHBTC",intraday=True)
    for interval in ("5m","1h"):
        g._validate_request("GET","/api/v3/klines",{"symbol":"ETHBTC","interval":interval,"limit":120},False)
    with pytest.raises(GatewayError):
        g._validate_request("GET","/api/v3/klines",{"symbol":"ETHBTC","interval":"1s","limit":120},False)


def test_coin_transport_reads_all_chunks_before_json_parsing():
    class Content:
        async def iter_chunked(self, _size):
            for part in (b'[[1,"2",',b'"3",',b'"1","2",0,9]]'):
                await asyncio.sleep(0)
                yield part
    class Response:
        status, headers, content = 200, {}, Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
    transport = CoinTransport()
    transport.session = SimpleNamespace(request=lambda *_args, **_kwargs: Response())
    response = asyncio.run(transport.request("GET","https://dapi.binance.com/dapi/v1/klines",{},10))
    assert response.text == '[[1,"2","3","1","2",0,9]]'
