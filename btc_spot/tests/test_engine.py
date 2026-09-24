import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal as D
import json

import pytest

from btc_spot.engine import Engine, plan_order, validate_plan_filters
from btc_spot.store import Store


NOW = 1_800_000_000_000
DAY = 86_400_000


def run(value):
    return asyncio.run(value)


def date(now=NOW):
    return datetime.fromtimestamp(now/1000, timezone.utc).date().isoformat()


def market(now=NOW):
    return {"symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT", "status": "TRADING",
            "spot_allowed": True, "server_time_ms": now, "received_at_ms": now,
            "bid": "50000", "ask": "50000", "reference_price": "50000", "klines": [],
            "filters": [{"filterType": "PRICE_FILTER", "minPrice": ".01", "maxPrice": "10000000", "tickSize": ".01"},
                        {"filterType": "LOT_SIZE", "minQty": ".00001", "maxQty": "100", "stepSize": ".00001"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "100", "stepSize": "0"},
                        {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "10000000",
                         "applyMinToMarket": False, "applyMaxToMarket": False, "avgPriceMins": 5}],
            "account_filters": {"exchangeFilters": [], "symbolFilters": [], "assetFilters": [], "rateLimits": []}}


class DefiniteRejection(RuntimeError):
    code = -2010
    maybe_sent = False


class LocalSubmissionStop(RuntimeError):
    code = None
    maybe_sent = False
    definitely_not_submitted = True


class FakeGateway:
    def __init__(self):
        self.btc, self.quote = D(".004"), D("10")
        self.uid = 42
        self.post_calls = []
        self.orders, self.fills = {}, {}
        self.partial = self.timeout = self.crash = self.reject = False
        self.trade_visibility = None
        self.foreign_orders = []
        self.fee_asset = None
        self.now = NOW

    async def market(self):
        return market(self.now)

    async def account(self):
        locked = sum((D(o["origQty"])-D(o["executedQty"]) for o in self.orders.values()
                      if o["side"] == "SELL" and o["status"] == "PARTIALLY_FILLED"), D(0))
        return {"uid": self.uid, "canTrade": True,
                "balances": [{"asset": "BTC", "free": str(self.btc-locked), "locked": str(locked)},
                             {"asset": "USDT", "free": str(self.quote), "locked": "0"}]}

    async def open_orders(self, *, all_symbols=False):
        assert all_symbols
        return deepcopy(self.foreign_orders + [o for o in self.orders.values() if o["status"] == "PARTIALLY_FILLED"])

    def add_fill(self, order, quantity, *, terminal=True):
        price = D("50000")
        cost = quantity*price
        asset = self.fee_asset or ("BTC" if order["side"] == "BUY" else "USDT")
        fee = (quantity if asset == "BTC" else cost if asset == "USDT" else D(".01"))*D(".001")
        if order["side"] == "BUY":
            self.btc += quantity
            self.quote -= cost
        else:
            self.btc -= quantity
            self.quote += cost
        if asset == "BTC":
            self.btc -= fee
        elif asset == "USDT":
            self.quote -= fee
        trade_id = sum(map(len, self.fills.values())) + 1
        self.fills.setdefault(order["orderId"], []).append({"id": trade_id, "orderId": order["orderId"],
            "symbol": "BTCUSDT", "qty": str(quantity), "price": str(price), "quoteQty": str(cost),
            "commission": str(fee), "commissionAsset": asset, "isBuyer": order["side"] == "BUY"})
        order["executedQty"] = str(D(order["executedQty"])+quantity)
        order["cummulativeQuoteQty"] = str(D(order["cummulativeQuoteQty"])+cost)
        order["status"] = "FILLED" if terminal else "PARTIALLY_FILLED"

    async def place_order(self, client_id, side, quantity, *, limit_price=None):
        assert limit_price is not None
        self.post_calls.append((client_id, side, quantity, limit_price))
        if self.reject:
            raise DefiniteRejection("signed url?signature=NEVER_PERSIST_THIS")
        if self.timeout:
            raise TimeoutError("signed url?signature=NEVER_PERSIST_THIS")
        order = {"symbol": "BTCUSDT", "orderId": len(self.orders)+1, "clientOrderId": client_id,
                 "side": side, "type": "LIMIT", "timeInForce": "IOC", "price": str(limit_price),
                 "origQty": str(quantity), "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW"}
        self.orders[client_id] = order
        self.add_fill(order, quantity/2 if self.partial else quantity, terminal=not self.partial)
        if self.crash:
            raise SystemExit("process termination after accepted POST")
        return deepcopy(order)

    async def get_order(self, client_id):
        return deepcopy(self.orders.get(client_id))

    async def trades(self, order_id):
        rows = deepcopy(self.fills.get(order_id, []))
        return rows if self.trade_visibility is None else rows[:self.trade_visibility]


def store(path, **kw):
    return Store(path, "paper", D(".003"), strategy_fingerprint="test-spot-ioc-v1", **kw)


def engine(gateway, storage, **kw):
    return Engine(gateway, storage, clock_ms=lambda: gateway.now, **kw)


def test_once_per_day_noop_and_reserve_are_durable(tmp_path):
    gateway = FakeGateway()
    path = tmp_path / "ledger.sqlite"
    with store(path) as storage:
        bot = engine(gateway, storage)
        first = run(bot.execute(date(), D(1), market()))
        assert first["status"] == "NOOP"
        assert storage.wallet()["reserve_btc"] == "0.001"
        assert storage.wallet()["reserve_quote"] == "10"
    with store(path) as storage:
        assert run(engine(gateway, storage).execute(date(), D(1), market()))["status"] == "ALREADY_DECIDED"
    assert gateway.post_calls == []


def test_received_asset_fees_and_allocation_reserves_reconcile(tmp_path):
    gateway = FakeGateway()
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        assert run(bot.execute(date(), D(0), market()))["status"] == "COMPLETE"
        assert storage.fill_count() == 1
        assert D(storage.wallet()["reserve_btc"]) == D(".001")
        assert D(storage.wallet()["reserve_quote"]) == 10
        gateway.now += DAY
        assert run(bot.execute(date(gateway.now), D(1), market(gateway.now)))["status"] == "COMPLETE"
        wallet = storage.wallet()
        assert D(wallet["btc"]) + D(wallet["reserve_btc"]) == gateway.btc
        assert D(wallet["quote"]) + D(wallet["reserve_quote"]) == gateway.quote
        assert gateway.quote >= 10
        assert set(storage.fees()) == {"BTC", "USDT"}
        before = (storage.wallet(), storage.fill_count())
        assert run(bot.recover())["status"] == "READY"
        assert (storage.wallet(), storage.fill_count()) == before


def test_crash_after_accepted_post_recovers_without_second_post(tmp_path):
    gateway = FakeGateway()
    gateway.crash = True
    path = tmp_path / "ledger.sqlite"
    with store(path) as storage:
        with pytest.raises(SystemExit):
            run(engine(gateway, storage).execute(date(), D(0), market()))
        assert len(storage.pending()) == 1
        assert storage.fill_count() == 0
    with store(path) as restarted:
        outcome = run(engine(gateway, restarted).recover())
        assert outcome["status"] == "READY"
        assert not restarted.pending()
        assert restarted.fill_count() == 1
    assert len(gateway.post_calls) == 1


def test_unknown_submit_never_reposts_or_allows_later_decisions(tmp_path):
    gateway = FakeGateway()
    gateway.timeout = True
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        assert run(bot.execute(date(), D(0), market()))["reason"] == "order_outcome_unknown_no_resubmit"
        gateway.now += DAY
        assert run(bot.execute(date(gateway.now), D(0), market(gateway.now)))["status"] == "BLOCKED"
        assert len(gateway.post_calls) == 1
        assert "NEVER_PERSIST_THIS" not in json.dumps(bot.status())


def test_definite_rejection_finishes_day_without_retry(tmp_path):
    gateway = FakeGateway()
    gateway.reject = True
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        assert run(bot.execute(date(), D(0), market()))["status"] == "REJECTED"
        assert not storage.pending()
        assert run(bot.execute(date(), D(0), market()))["status"] == "ALREADY_DECIDED"
        assert len(gateway.post_calls) == 1


@pytest.mark.parametrize("query_failure", [TimeoutError, SystemExit])
def test_definite_rejection_survives_failed_query_and_restart(tmp_path, query_failure):
    class QueryFailureGateway(FakeGateway):
        async def get_order(self, client_id):
            raise query_failure("failed after definite rejection")

    gateway = QueryFailureGateway()
    gateway.reject = True
    path = tmp_path / "ledger.sqlite"
    with store(path) as storage:
        if query_failure is SystemExit:
            with pytest.raises(SystemExit):
                run(engine(gateway, storage).execute(date(), D(0), market()))
        else:
            assert run(engine(gateway, storage).execute(date(), D(0), market()))["status"] == "BLOCKED"
        assert storage.pending()[0]["order_status"] == "REJECTED_BEFORE_ACCEPTANCE"
    gateway.get_order = FakeGateway.get_order.__get__(gateway)
    with store(path) as storage:
        bot = engine(gateway, storage)
        assert run(bot.recover())["status"] == "READY"
        assert not storage.pending()
        assert storage.latest_decision()["order_status"] == "REJECTED"
        assert run(bot.execute(date(), D(0), market()))["status"] == "ALREADY_DECIDED"
        assert len(gateway.post_calls) == 1


def test_gateway_stop_after_engine_check_completes_without_pending(tmp_path):
    class StopInGateway(FakeGateway):
        async def place_order(self, client_id, side, quantity, *, limit_price=None):
            # Models the final gateway guard after asynchronous clock sync.
            raise LocalSubmissionStop("stop detected before transport POST")

    gateway = StopInGateway()
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        assert run(bot.execute(date(), D(0), market()))["status"] == "STOPPED"
        assert storage.latest_decision()["order_status"] == "CANCELED_BEFORE_SUBMIT"
        assert not storage.pending()
        assert run(bot.execute(date(), D(0), market()))["status"] == "ALREADY_DECIDED"
        assert not gateway.post_calls


def test_partial_fills_book_once_and_pending_blocks_new_decisions(tmp_path):
    gateway = FakeGateway()
    gateway.partial = True
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        assert run(bot.execute(date(), D(0), market()))["reason"] == "order_still_open"
        first_wallet = storage.wallet()
        run(bot.recover())
        assert storage.wallet() == first_wallet and storage.fill_count() == 1
        order = next(iter(gateway.orders.values()))
        gateway.add_fill(order, D(order["origQty"])-D(order["executedQty"]))
        assert run(bot.recover())["status"] == "READY"
        assert storage.fill_count() == 2
        assert len(gateway.post_calls) == 1


def test_delayed_trade_history_is_not_mistaken_for_balance_drift(tmp_path):
    gateway = FakeGateway()
    gateway.trade_visibility = 0
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        assert run(bot.execute(date(), D(0), market()))["reason"] == "trade_history_not_yet_complete"
        assert storage.fill_count() == 0
        gateway.trade_visibility = None
        assert run(bot.recover())["status"] == "READY"
        assert storage.fill_count() == 1


def test_unexpected_fee_asset_is_recorded_and_blocks_new_orders(tmp_path):
    gateway = FakeGateway()
    gateway.fee_asset = "BNB"
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        result = run(bot.execute(date(), D(0), market()))
        assert result["reason"] == "unsupported_fee_asset_cost_recorded"
        assert D(storage.fees()["BNB"]) > 0
        assert storage.fill_count() == 1
        gateway.now += DAY
        run(bot.execute(date(gateway.now), D(1), market(gateway.now)))
        assert len(gateway.post_calls) == 1


def test_account_drift_foreign_orders_and_uid_change_block(tmp_path):
    gateway = FakeGateway()
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        run(bot.execute(date(), D(1), market()))
        gateway.btc += D(".001")
        assert run(bot.recover())["reason"] == "account_balance_drift"
        gateway.btc -= D(".001")
        gateway.foreign_orders = [{"symbol": "ETHUSDT", "orderId": 999}]
        assert run(bot.recover())["reason"] == "foreign_or_unresolved_open_orders"
        gateway.foreign_orders = []
        gateway.uid = 100
        assert run(bot.recover())["reason"] == "account_uid_changed"
        assert gateway.post_calls == []


def test_stop_before_and_after_intent_never_submits(tmp_path):
    gateway = FakeGateway()
    with store(tmp_path / "before.sqlite") as storage:
        bot = engine(gateway, storage, stop_requested=lambda: True)
        assert run(bot.execute(date(), D(0), market()))["status"] == "STOPPED"
        assert storage.latest_decision() is None
    calls = []
    def stop_second_time():
        calls.append(1)
        return len(calls) == 2
    with store(tmp_path / "after.sqlite") as storage:
        bot = engine(gateway, storage, stop_requested=stop_second_time)
        assert run(bot.execute(date(), D(0), market()))["status"] == "STOPPED"
        assert storage.latest_decision()["order_status"] == "CANCELED_BEFORE_SUBMIT"
        assert not storage.pending()
    assert gateway.post_calls == []


def test_ioc_price_budget_bounds_and_limit_notional_rules():
    quote, btc, fee = D("150"), D(".003"), D(".001")
    m = market()
    m["ask"] = "50000.013"
    buy = plan_order(btc_balance=0, quote_balance=quote, target_btc_fraction=1, market=m, fee_rate=fee)
    sell = plan_order(btc_balance=btc, quote_balance=0, target_btc_fraction=0, market=m, fee_rate=fee)
    assert D(buy["quantity"])*D(buy["limit_price"])*(1+fee) <= quote
    assert D(sell["quantity"])*(1+fee) <= btc
    assert D(buy["limit_price"]) >= D(m["ask"])*D("1.0003")
    assert D(sell["limit_price"]) <= D(m["bid"])*D(".9997")
    for plan in (buy, sell):
        assert not ({"execution_price", "notional", "btc_after", "quote_after", "fee_quote",
                     "fee_btc", "fee_quote_equivalent", "btc_delta", "quote_delta"} & plan.keys())
        assert plan["fee_budget_assumption"] == "BTC_or_USDT_worst_case"
    m["filters"][-1]["minNotional"] = "200"
    assert plan_order(btc_balance=btc, quote_balance=0, target_btc_fraction=0, market=m)["status"] == "SKIP"


def test_max_asset_order_cap_and_max_position_include_reserve():
    m = market()
    sized = plan_order(btc_balance=0, quote_balance=150, target_btc_fraction=1, market=m)
    balances = {"BTC": {"total": D(".004")}}
    m["account_filters"]["assetFilters"] = [{"filterType": "MAX_ASSET", "asset": "BTC", "limit": ".002"}]
    with pytest.raises(ValueError, match="MAX_ASSET"):
        validate_plan_filters(m, sized, balances)
    m["account_filters"]["assetFilters"] = []
    m["filters"].append({"filterType": "MAX_POSITION", "maxPosition": ".006"})
    with pytest.raises(ValueError, match="MAX_POSITION"):
        validate_plan_filters(m, sized, balances)


def test_state_binding_and_os_lock_reject_reuse(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with store(path):
        with pytest.raises(RuntimeError):
            store(path)
    with pytest.raises(ValueError, match="State mode"):
        Store(path, "live", D(".003"), strategy_fingerprint="test-spot-ioc-v1")
    with pytest.raises(ValueError, match="State mode"):
        Store(path, "paper", D(".004"), strategy_fingerprint="test-spot-ioc-v1")


def test_stale_market_and_old_day_cannot_create_intent(tmp_path):
    gateway = FakeGateway()
    with store(tmp_path / "ledger.sqlite") as storage:
        bot = engine(gateway, storage)
        old = market(NOW-DAY)
        assert run(bot.execute(date(NOW-DAY), D(0), old))["status"] == "BLOCKED"
        assert storage.latest_decision() is None
        assert gateway.post_calls == []
