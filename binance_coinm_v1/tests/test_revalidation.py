"""Independent audit scenarios beyond the original happy-path tests."""

from decimal import Decimal

import pytest

from binance_coinm_v1.exchange.errors import NetworkError, OrderStatusUnknown
from binance_coinm_v1.exchange.models import Fill, OrderState

from .conftest import run
from .fakes import FakeTransport
from .harness import Harness
from .test_account_sync import gw
from .test_execution import long_open

NOW_MS = 1_790_000_000_000
DAY = 86_400_000


def test_history_fetches_more_than_1000_fills_at_same_timestamp():
    rows = [dict(symbol="BTCUSD_PERP", id=k, orderId=9, time=NOW_MS - 1000,
                 side="BUY", qty="1", price="80000", commission="0.000001",
                 commissionAsset="BTC", realizedPnl="0") for k in range(1, 1506)]

    def handler(p):
        assert not ("fromId" in p and ("startTime" in p or "endTime" in p))
        return [r for r in rows if r["id"] >= int(p.get("fromId", 0))][:1000]

    tr = FakeTransport().add("GET", "/dapi/v1/userTrades", handler)
    result = run(gw(tr).get_user_trades("BTCUSD_PERP", NOW_MS - DAY))
    assert len(result) == 1505 and len({f.trade_id for f in result}) == 1505


def test_income_fetches_all_pages_with_fixed_time_bounds():
    rows = [dict(symbol="BTCUSD_PERP", incomeType="FUNDING_FEE", income="-0.000001",
                 asset="BTC", time=NOW_MS - 1000, tranId=str(k)) for k in range(1505)]
    tr = FakeTransport().add("GET", "/dapi/v1/income",
                            lambda p: rows[(int(p.get("page", 1)) - 1) * 1000:int(p.get("page", 1)) * 1000])
    result = run(gw(tr).get_income("BTCUSD_PERP", "FUNDING_FEE", NOW_MS - DAY))
    assert len(result) == 1505
    assert all(c["params"]["endTime"] == str(NOW_MS) for c in tr.calls)


def test_mixed_absent_and_failed_queries_do_not_prove_order_absence(tmp_path):
    async def go():
        h = Harness(tmp_path)
        responses = iter([False, False, True, True, True])

        async def query(symbol, cid):
            if next(responses):
                raise NetworkError("query failed")
            return OrderState.not_found(cid, symbol)

        h.paper.get_order = query
        with pytest.raises(OrderStatusUnknown):
            await h.engine.ctx.resolve("cm1deadbeef00-EN0", False)
    run(go())


def test_previously_unassigned_fill_can_be_reconciled_once(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        f = Fill("BTCUSD_PERP", "late-fill", "777", "SELL", 81000, Decimal(1),
                 0.0001, 0.000001, "BTC", h.clock.t * 1000)
        h.engine.ctx.record_fill(f, "ws", None)
        assert h.db.unassigned_fills("paper", "BTCUSD_PERP")
        h.engine.ctx.record_fill(f, "rest", t)
        h.engine.ctx.record_fill(f, "rest", t)
        rows = [r for r in h.db.fills_for_trade(t.trade_id) if r["exchange_trade_id"] == "late-fill"]
        assert len(rows) == 1
        assert not h.db.unassigned_fills("paper", "BTCUSD_PERP")
    run(go())


def test_funding_is_valued_at_event_price_and_can_be_assigned_later(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        ft = int((h.clock.t + 1) * 1000)
        h.paper.income.append(dict(symbol="BTCUSD_PERP", incomeType="FUNDING_FEE",
                                   income="-0.00001", asset="BTC", time=ft, tranId="9",
                                   markPrice=80500))
        await h.tick(81000, dt=2)
        await h.engine.ctx.sync_funding(None, ft)
        await h.tick(82000)
        await h.engine.ctx.sync_funding(t, ft)
        events = h.db.funding_events("paper", t.trade_id)
        assert len(events) == 1
        assert events[0]["funding_fee_usd"] == pytest.approx(-0.805)
    run(go())


def test_unknown_fee_asset_is_not_multiplied_by_btc_price(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        f = Fill("BTCUSD_PERP", "foreign-fee", "888", "SELL", 81000, Decimal(1),
                 0.0001, 0.002, "BNB", int(h.clock.t * 1000))
        h.engine.ctx.record_fill(f, "ws", t)
        acc = h.engine.ctx.recompute_accounting(t)
        assert acc["net_pnl_usd"] is None
        assert acc["net_pnl_btc"] is None
        assert not acc["accounting_complete"]
    run(go())
