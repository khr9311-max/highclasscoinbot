"""Independent audit scenarios beyond the original happy-path tests."""

from decimal import Decimal
from pathlib import Path
import subprocess
import sys

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


@pytest.mark.parametrize("name", ["equity_btc", "available_btc", "entry_price", "stop_price",
                                   "funding_rate", "taker_fee", "mmr"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_sizing_rejects_nonfinite_exchange_values(name, value):
    from binance_coinm_v1.risk.sizing import SizingInput, size_position
    from .helpers import load_spec
    args = dict(equity_btc=0.05, available_btc=0.05, risk_fraction=0.005, direction=1,
                entry_price=80000, stop_price=79200, leverage=3, taker_fee=0.0005)
    args[name] = value
    assert not size_position(SizingInput(**args), load_spec()).ok


def test_random_sizing_obeys_independently_calculated_budget_and_margin():
    import random
    from binance_coinm_v1.risk.sizing import SizingInput, size_position
    from .helpers import load_spec
    rng, spec = random.Random(2431), load_spec()
    accepted = 0
    for _ in range(2000):
        eq = 10 ** rng.uniform(-3.5, 1)
        entry = rng.uniform(5000, 200000)
        direction = rng.choice([-1, 1])
        stop = entry * (1 - direction * rng.uniform(0.002, 0.08))
        inp = SizingInput(equity_btc=eq, available_btc=eq * rng.uniform(0.05, 1),
                          risk_fraction=rng.uniform(0.001, 0.01), direction=direction,
                          entry_price=entry, stop_price=stop, leverage=rng.choice([1, 2, 3]),
                          taker_fee=0.0005, entry_slippage_bps=1, stop_slippage_bps=3)
        result = size_position(inp, spec)
        if not result.ok:
            continue
        accepted += 1
        q, cs = float(result.qty), float(spec.contract_size)
        entry_eff, stop_eff = entry * (1 + direction * 0.0001), stop * (1 - direction * 0.0003)
        loss = q * cs * (abs(1 / stop_eff - 1 / entry_eff) + inp.taker_fee * (1 / stop_eff + 1 / entry_eff))
        required = q * cs / entry_eff * (1 / inp.leverage + inp.taker_fee)
        assert loss <= eq * inp.risk_fraction + 1e-12
        assert required <= inp.available_btc + 1e-12
        assert q * cs / entry_eff <= eq * inp.max_exposure_multiple + 1e-12
        assert result.qty % spec.step_size == 0
    assert accepted > 500


def test_history_splits_seven_day_windows_and_never_loses_boundary_fills():
    start = NOW_MS - 20 * DAY
    rows = [dict(symbol="BTCUSD_PERP", id=k, orderId=9, time=start + k * DAY,
                 side="BUY", qty="1", price="80000", commission="0.000001",
                 commissionAsset="BTC", realizedPnl="0") for k in range(21)]
    def handler(p):
        assert int(p["endTime"]) - int(p["startTime"]) < 7 * DAY
        return [r for r in rows if int(p["startTime"]) <= r["time"] <= int(p["endTime"])]
    tr = FakeTransport().add("GET", "/dapi/v1/userTrades", handler)
    result = run(gw(tr).get_user_trades("BTCUSD_PERP", start))
    assert len(result) == 21 and len(tr.calls) == 3


def test_income_duplicate_full_page_raises_instead_of_returning_partial_history():
    from binance_coinm_v1.exchange.errors import ExchangeError
    rows = [dict(symbol="BTCUSD_PERP", incomeType="FUNDING_FEE", income="0.000001",
                 asset="BTC", time=NOW_MS - 1000, tranId=k) for k in range(1000)]
    tr = FakeTransport().add("GET", "/dapi/v1/income", lambda p: rows)
    with pytest.raises(ExchangeError, match="pagination"):
        run(gw(tr).get_income("BTCUSD_PERP", "FUNDING_FEE", NOW_MS - DAY))
    assert len(tr.calls) == 2


def test_disk_checkpoint_failure_does_not_publish_uncommitted_fills(tmp_path):
    from binance_coinm_v1.exchange.models import OrderRequest
    async def go():
        h = Harness(tmp_path)
        await h.start()
        published = []
        h.paper.set_event_sink(published.append)
        def fail(state):
            raise OSError("disk full")
        h.paper.checkpoint = fail
        with pytest.raises(OSError, match="disk full"):
            await h.paper.place_order(OrderRequest("disk-failure", h.spec.symbol, "BUY", "MARKET",
                                                  quantity=Decimal(1)))
        assert not published
    run(go())


@pytest.mark.parametrize("stage", ["entry", "entry_response", "tp", "stop", "cancel", "funding"])
def test_hard_exit_recovers_from_disk_without_duplicate_execution(tmp_path, stage):
    code = '''
import asyncio, os, sys
from pathlib import Path
from binance_coinm_v1.tests.harness import Harness
from binance_coinm_v1.tests.test_execution import long_open

async def go():
    h = Harness(Path(sys.argv[1]))
    stage = sys.argv[2]
    def checkpoint(state):
        h.db.kv_set("paper:paper_state", state)
        if stage == "entry" and state["fills"]:
            os._exit(73)  # after exchange persistence, before engine sees response
    h.paper.checkpoint = checkpoint
    original_store = h.engine.ctx.store_state
    def store(order):
        original_store(order)
        if stage == "entry_response" and order.status == "FILLED":
            os._exit(73)  # order DB committed, trade has not adopted the fill yet
    h.engine.ctx.store_state = store
    t = await long_open(h)
    if stage in ("tp", "stop"):
        px = 81100 if stage == "tp" else 79100
        h.paper.update_market(last=px, mark=px, ts_ms=int((h.clock.t + 1) * 1000))
    elif stage == "cancel":
        await h.paper.cancel_order(h.spec.symbol, t.orders["tp0"], True)
    elif stage == "funding":
        ft = int((h.clock.t + 1) * 1000)
        h.paper.update_market(last=80150, mark=80150, ts_ms=ft,
                              funding_rate=0.0001, next_funding_ms=ft)
    os._exit(73)  # no engine drain, periodic snapshot, finally, or graceful shutdown
asyncio.run(go())
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), stage],
                            capture_output=True, text=True, timeout=30,
                            cwd=str(Path(__file__).resolve().parents[2]))
    assert result.returncode == 73, result.stderr

    async def recover():
        from binance_coinm_v1.storage import Database
        from binance_coinm_v1.exchange.paper_gateway import PaperGateway
        from .harness import Clock
        from .helpers import load_spec
        from binance_coinm_v1.config import Settings
        db = Database(str(tmp_path / "engine.sqlite3"))
        try:
            state = db.kv_get("paper:paper_state")
            clock = Clock(state["market_ts_ms"] / 1000 + 1)
            paper = PaperGateway(load_spec(), Settings.build(), clock=clock)
            paper.load_state(state)
            qty, wallet, n = paper.pos_qty, paper.wallet, len(paper.fills)
            h = Harness(tmp_path, db=db, paper=paper, clock=clock)
            await h.start(81100 if stage == "tp" else (79100 if stage == "stop" else 80150))
            await h.engine.reconcile("repeat_disk_recovery")
            assert h.position() == qty and len(paper.fills) == n
            assert paper.wallet == pytest.approx(wallet, abs=1e-12)
            if qty:
                assert h.trade and h.trade.entry_fill_time_ms == paper.fills[0].time_ms
                assert len([a for a in h.algos() if a["type"] == "STOP_MARKET"]) == 1
                if stage == "tp":
                    assert 0 in h.trade.tp_filled
                if stage == "funding":
                    events = db.funding_events("paper", h.trade.trade_id)
                    assert len(events) == 1 and events[0]["mark_price"] == 80150
            else:
                closed = db.closed_positions("paper")
                assert len(closed) == 1 and closed[0]["accounting"]["accounting_complete"]
                assert not h.algos()
        finally:
            db.close()
    run(recover())


def test_instance_lock_blocks_other_process_and_releases_after_hard_exit(tmp_path):
    from binance_coinm_v1.runtime.instance_lock import InstanceLock
    lock = InstanceLock(tmp_path / "bot.lock")
    lock.acquire()
    code = '''
import os, sys
from binance_coinm_v1.runtime.instance_lock import InstanceLock
lock = InstanceLock(sys.argv[1])
lock.acquire()
os._exit(73)
'''
    try:
        p = subprocess.run([sys.executable, "-c", code, str(lock.path)],
                           capture_output=True, timeout=10)
        assert p.returncode == 1
    finally:
        lock.release()
    p = subprocess.run([sys.executable, "-c", code, str(lock.path)],
                       capture_output=True, timeout=10)
    assert p.returncode == 73
    lock.acquire()
    lock.release()


def test_runtime_lock_prevents_second_bot_before_network_and_releases_on_failure(tmp_path):
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.runtime.bot import Bot
    from binance_coinm_v1.notifications import RecordingNotifier
    from .test_runtime import fake_exchange
    async def go():
        s = Settings.build(state_dir=str(tmp_path))
        first = Bot(s, transport=fake_exchange(), notifier=RecordingNotifier())
        second_tr = fake_exchange()
        second = Bot(s, transport=second_tr, notifier=RecordingNotifier())
        try:
            await first.setup()
            with pytest.raises(RuntimeError, match="DB"):
                await second.run(duration=0.01)
            assert not second_tr.calls
        finally:
            await first.shutdown()
        third = Bot(s, transport=fake_exchange(), notifier=RecordingNotifier())
        try:
            await third.setup()
        finally:
            await third.shutdown()
    run(go())


@pytest.mark.parametrize("price,age", [(79000, -1000), (float("nan"), 1000),
                                       (float("inf"), 1000), (-1, 1000)])
def test_paper_ignores_stale_or_invalid_prices_before_triggering_orders(tmp_path, price, age):
    async def go():
        h = Harness(tmp_path)
        await long_open(h)
        qty, n, mark = h.position(), len(h.paper.fills), h.paper.mark
        h.paper.update_market(last=price, mark=price, ts_ms=int(h.clock.t * 1000) + age)
        assert h.position() == qty and len(h.paper.fills) == n and h.paper.mark == mark
    run(go())


def test_missing_funding_price_keeps_btc_but_does_not_invent_usd(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        h.paper.income.append(dict(symbol=h.spec.symbol, incomeType="FUNDING_FEE", asset="BTC",
                                   income="-0.00001", time=int(h.clock.t * 1000), tranId="987"))
        await h.engine.ctx.sync_funding(t)
        a = h.engine.ctx.recompute_accounting(t)
        assert a["accounting_complete"] and a["net_pnl_btc"] is not None
        assert not a["usd_accounting_complete"] and a["net_pnl_usd"] is None
    run(go())


def test_funding_multiple_transactions_at_same_time_are_summed_once(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        for k in range(3):
            h.paper.income.append(dict(symbol=h.spec.symbol, incomeType="FUNDING_FEE", asset="BTC",
                                       income="-0.00001", time=int(h.clock.t * 1000), tranId=str(k),
                                       markPrice=80150))
        await h.engine.ctx.sync_funding(t)
        await h.engine.ctx.sync_funding(t)
        ev = h.db.funding_events("paper", t.trade_id)
        assert len(ev) == 1 and ev[0]["funding_fee_btc"] == pytest.approx(-0.00003)
    run(go())


def test_close_with_failed_history_query_stays_accounting_unconfirmed(tmp_path):
    from binance_coinm_v1.exchange.paper_gateway import Fault
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        h.paper.inject(Fault("get_income", NetworkError("offline"), times=50))
        await h.tick(79100)
        row = h.db.get_position(t.trade_id)
        assert row["state"] == "CLOSED"
        assert not row["accounting"]["accounting_complete"]
        assert row["accounting"]["net_pnl_btc"] is None
        assert "미확정" in h.notes.messages[-1][1]
    run(go())


def test_rest_fill_before_current_entry_is_not_assigned_to_current_trade(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        f = Fill(h.spec.symbol, "old-unrelated", "unowned", "SELL", 80000, Decimal(1),
                 0.01, 0.000001, "BTC", t.entry_fill_time_ms - 1)
        assert h.engine.ctx.record_fill(f, "rest", t) is None
        assert h.db.unassigned_fills("paper", h.spec.symbol)[0]["exchange_trade_id"] == f.trade_id
    run(go())


def test_liquidation_insurance_fee_matches_wallet_and_fill_ledger(tmp_path):
    from binance_coinm_v1.exchange.models import OrderRequest
    async def go():
        h = Harness(tmp_path)
        await h.start()
        before = h.paper.wallet
        await h.paper.place_order(OrderRequest("manual-test", h.spec.symbol, "BUY", "MARKET",
                                              quantity=Decimal(3)))
        lp = (await h.paper.get_position(h.spec.symbol)).liquidation_price
        h.paper.update_market(last=lp * 0.99, mark=lp * 0.99, ts_ms=int(h.clock.t * 1000) + 1)
        assert h.paper.pos_qty == 0 and h.paper.fills[-1].commission > 0
        net = sum(f.realized_pnl_btc - f.commission for f in h.paper.fills)
        assert h.paper.wallet - before == pytest.approx(net, abs=1e-12)
    run(go())


def test_long_offline_paper_funding_estimate_is_not_confirmed_accounting(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        ft = int((h.clock.t + 1) * 1000)
        h.paper.update_market(last=80150, mark=80150, ts_ms=ft + 60001,
                              funding_rate=0.0001, next_funding_ms=ft)
        await h.engine.ctx.sync_funding(t)
        a = h.engine.ctx.recompute_accounting(t)
        assert not a["accounting_complete"] and "estimated_paper_funding" in a["accounting_errors"]
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
