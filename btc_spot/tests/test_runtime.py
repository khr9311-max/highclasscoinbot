import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from btc_spot.paper import PaperGateway
from btc_spot.runtime import private_checks, run_loop, safe_error, valuation
from btc_spot.strategy import DAY_MS


class Public:
    has_credentials = False
    allow_orders = False

    async def market(self):
        now = int(time.time()*1000)
        day = now//DAY_MS*DAY_MS
        rows = []
        for i in range(202):
            stamp = day - (201-i)*DAY_MS
            close = str(50000-i)
            rows.append([stamp, close, close, close, close, "1", stamp+DAY_MS-1,
                         "1", 1, "1", "1", "0"])
        return {"server_time_ms": now, "received_at_ms": now, "symbol": "BTCUSDT",
                "base_asset": "BTC", "quote_asset": "USDT", "status": "TRADING", "spot_allowed": True,
                "bid": "50000", "ask": "50000.01", "bid_qty": "1", "ask_qty": "1",
                "reference_price": "50000", "klines": rows, "filters": [
                    {"filterType":"PRICE_FILTER", "tickSize":"0.01", "minPrice":"0.01", "maxPrice":"1000000"},
                    {"filterType":"LOT_SIZE", "minQty":"0.00001", "maxQty":"9000", "stepSize":"0.00001"},
                    {"filterType":"MARKET_LOT_SIZE", "minQty":"0", "maxQty":"100", "stepSize":"0"},
                    {"filterType":"NOTIONAL", "minNotional":"5", "maxNotional":"9000000",
                     "applyMinToMarket":True, "applyMaxToMarket":False, "avgPriceMins":5}]}

    async def close(self):
        pass


def test_full_runtime_paper_restart_is_one_fill(tmp_path):
    async def once():
        gateway = PaperGateway(Public(), tmp_path/"paper_exchange.json")
        return await run_loop(gateway, tmp_path, "paper", Decimal(".003"), once=True)
    first = asyncio.run(once())
    assert first["status"] == "COMPLETE", first
    assert first["ledger"]["fill_count"] == 1
    assert Decimal(first["ledger"]["wallet"]["quote"]) > 0
    second = asyncio.run(once())
    assert second["status"] == "ALREADY_DECIDED", second
    assert second["ledger"]["fill_count"] == 1
    assert second["ledger"]["wallet"] == first["ledger"]["wallet"]
    assert second["valuation"]["held_strategy_btc"] == second["ledger"]["wallet"]["btc"]


class Private:
    async def account(self):
        return {"canTrade": True, "balances":[{"asset":"BTC", "free":"0.00412273", "locked":"0"}]}
    async def permissions(self):
        return {"enableSpotAndMarginTrading":False}
    async def open_orders(self, **kwargs):
        return []
    async def commission_rate(self):
        return Decimal(".001")
    async def relevant_filters(self):
        return {"exchangeFilters":[], "symbolFilters":[], "assetFilters":[]}


def test_spot_permission_blocker_and_post_sale_balance():
    result = asyncio.run(private_checks(Private(), Decimal(".003")))
    assert result["blockers"] == ["ENABLE_SPOT_TRADING_PERMISSION"]
    # After allocation switches to quote, initial BTC is not a recurring deposit requirement.
    result = asyncio.run(private_checks(Private(), Decimal("1"), initial=False))
    assert "INSUFFICIENT_FREE_BTC_FOR_ALLOCATION" not in result["blockers"]


def test_live_requires_explicit_start_before_reading_credentials(tmp_path):
    from btc_spot.__main__ import dispatch
    args = SimpleNamespace(state_dir=tmp_path, mode="live", action="run", confirm="",
                           credentials_file=tmp_path/"missing.env")
    with pytest.raises(ValueError, match="Live start requires"):
        asyncio.run(dispatch(args))


def test_error_output_never_echoes_signed_url():
    result = safe_error(RuntimeError("https://api.binance.com/?signature=secret"))
    assert result == {"error_type":"RuntimeError"}


def test_error_output_reports_candle_shape_without_market_data():
    from btc_portfolio.signals import closed
    try:
        closed([None], 86_400_000, 86_400_000, 1)
    except ValueError as exc:
        result = safe_error(exc)
    assert result["error_type"] == "ValueError"
    assert result["error_site"].startswith("btc_portfolio.signals:")
    assert result["candle_shape"] == {"period_ms": 86_400_000, "container": "list",
                                      "count": 1, "first_bad_index": 0, "row_type": "NoneType"}


def test_valuation_distinguishes_actual_btc_and_quote():
    value = valuation({"wallet":{"btc":".001", "quote":"100", "reserve_btc":".00112273", "reserve_quote":"0"}},
                      {"ask":"50000"}, Decimal(".001"))
    assert Decimal(value["estimated_strategy_btc_equivalent_after_conversion_fee"]) == Decimal(".002998")
    assert Decimal(value["held_strategy_btc"]) == Decimal(".001")


@pytest.mark.parametrize("invalid", [None, "market", "asset_filter"])
def test_preparation_uses_actual_ioc_plan_without_orders(tmp_path, monkeypatch, invalid):
    import btc_spot.runtime as runtime
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "fingerprint", lambda: "fixed-test-version")
    class Prepared(Public, Private):
        async def account(self):
            return {**await super().account(), "uid": 123}
        async def permissions(self):
            return {"enableSpotAndMarginTrading": True}
        async def market(self):
            result = await super().market()
            result["spot_allowed"] = invalid != "market"
            return result
        async def relevant_filters(self):
            result = await super().relevant_filters()
            if invalid == "asset_filter":
                result["assetFilters"] = [{"filterType":"MAX_ASSET", "asset":"BTC", "limit":"0.00001"}]
            return result
        async def place_order(self, *args, **kwargs):
            pytest.fail("Read-only preparation must not submit orders")
    result = asyncio.run(runtime.prepare(Prepared(), Decimal(".003"), tmp_path/"live/readiness.json"))
    assert result["ready_for_explicit_live_start"] is (invalid is None)
    assert result["orders_submitted"] == 0
    plan = result["initial_order_preview"]
    assert plan["order_type"] == "LIMIT" and plan["time_in_force"] == "IOC"
    assert Decimal(plan["maximum_btc_debit_including_fee"]) <= Decimal(".003")
