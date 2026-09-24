import asyncio
from copy import deepcopy
from decimal import Decimal as D
import json
import time
from types import SimpleNamespace

import pytest

from binance_coinm_v1.exchange.models import AccountInfo, AssetBalance, PositionInfo, OrderState, Fill
from btc_portfolio.config import Config
from btc_portfolio.engine import Engine, coin_plan
from btc_portfolio.signals import closed, alt_signal, coinm_signal
from btc_portfolio.spot_gateway import SpotGateway, GatewayError
from btc_portfolio.store import Store
from btc_portfolio.venues import Venues


def run(coro):
    return asyncio.run(coro)


def candles(period, count, slope=1):
    end = int(time.time()*1000)//period*period
    result = []
    for i in range(count):
        price = D(1000)+slope*i
        t = end-(count-i)*period
        result.append([t, str(price), str(price+1), str(price-1), str(price), "1", t+period-1, "1", 1, "1", "1", "0"])
    return result


def spot_market():
    now = int(time.time()*1000)
    return {"symbol": "ETHBTC", "base_asset": "ETH", "quote_asset": "BTC", "status": "TRADING",
        "spot_allowed": True, "bid": ".03", "ask": ".03", "reference_price": ".03", "received_at_ms": now,
        "server_time_ms": now, "klines": candles(86_400_000, 61),
        "filters": [{"filterType": "PRICE_FILTER", "minPrice": ".000001", "maxPrice": "100", "tickSize": ".000001"},
                    {"filterType": "LOT_SIZE", "minQty": ".0001", "maxQty": "100", "stepSize": ".0001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": ".00005", "applyToMarket": True, "avgPriceMins": 5}],
        "account_filters": {"exchangeFilters": [], "symbolFilters": [], "assetFilters": []}}


class FakeCoin:
    def __init__(self, parent):
        self.parent = parent
        self.orders, self.trades, self.stops = {}, {}, {}
        self.reject_stop = False

    async def get_order(self, symbol, ident):
        return self.orders.get(ident, OrderState.not_found(ident, symbol))

    async def get_user_trades(self, symbol, start_ms=None, order_id=None):
        return self.trades.get(str(order_id), [])

    async def get_algo_order(self, symbol, ident):
        return self.stops.get(ident, OrderState.not_found(ident, symbol, True))

    async def cancel_order(self, symbol, ident, algo):
        self.stops[ident].status = "CANCELED"
        return self.stops[ident]


class FakeSpot:
    def __init__(self, parent):
        self.parent = parent
        self.orders, self.rows = {}, {}
        self.visibility = True

    async def get_order(self, ident):
        return self.orders.get(ident)

    async def trades(self, ident):
        return deepcopy(self.rows.get(ident, [])) if self.visibility else []

    async def relevant_filters(self):
        return spot_market()["account_filters"]


class FakeVenues:
    def __init__(self):
        self.spots = {"ETHBTC": FakeSpot(self)}
        self.coin = FakeCoin(self)
        self.assets = {"BTC": D(".0022"), "ETH": D(".5")}
        self.position = D(0)
        self.posts = []
        self.partial = False
        self.timeout = False
        self.reject = False

    async def account(self):
        return {"spot": {"uid": 123, "canTrade": True, "balances": [{"asset": k, "free": str(v), "locked": "0"} for k,v in self.assets.items()]},
            "permissions": {"enableSpotAndMarginTrading": True, "enableFutures": True},
            "spot_orders": [], "coin_orders": [], "hedge_mode": False,
            "coin": AccountInfo({"BTC": AssetBalance("BTC", .0018, 0, .0018, .0018)}, []),
            "position": PositionInfo("BTCUSD_PERP", self.position, 80000, 80000, 0, 0, 3, "isolated", 0),
            "algos": [s for s in self.coin.stops.values() if s.status == "NEW"]}

    async def submit(self, ident, venue, req):
        self.posts.append((ident, venue, deepcopy(req)))
        if self.reject:
            raise TimeoutError()
        side = 1 if req["side"] == "BUY" else -1
        q = D(req["quantity"])*(D(".5") if self.partial else 1)
        if venue == "spot":
            price = D(req["price"])
            fee = q*price*D(".001")
            self.assets["ETH"] += side*q
            self.assets["BTC"] -= side*q*price + fee
            oid = len(self.posts)
            self.spots["ETHBTC"].orders[ident] = {"clientOrderId": ident, "symbol": "ETHBTC", "side": req["side"],
                "type": "LIMIT", "timeInForce": "IOC", "price": str(price), "origQty": req["quantity"],
                "executedQty": str(q), "cummulativeQuoteQty": str(q*price), "status": "EXPIRED" if self.partial else "FILLED", "orderId": oid}
            self.spots["ETHBTC"].rows[oid] = [{"id": oid, "orderId": oid, "symbol": "ETHBTC", "isBuyer": side > 0,
                "qty": str(q), "price": str(price), "quoteQty": str(q*price), "commission": str(fee), "commissionAsset": "BTC"}]
        else:
            self.position += side*q
            oid = str(len(self.posts))
            self.coin.orders[ident] = OrderState(ident, "BTCUSD_PERP", req["side"], "MARKET" if req.get("emergency") else "LIMIT",
                "FILLED", exchange_id=oid, orig_qty=D(req["quantity"]), executed_qty=q, reduce_only=req.get("reduce_only", False),
                raw={"price": req.get("price", "0")})
            self.coin.trades[oid] = [Fill("BTCUSD_PERP", oid, oid, req["side"], 80000, q, 0, .000001, "BTC", int(time.time()*1000))]
        if self.timeout:
            raise TimeoutError()

    async def stop(self, ident, side, price):
        if self.coin.reject_stop:
            raise ValueError("reject")
        self.coin.stops[ident] = OrderState(ident, "BTCUSD_PERP", side, "STOP_MARKET", "NEW", is_algo=True,
            trigger_price=float(price), close_position=True, working_type="MARK_PRICE")


@pytest.fixture
def setup(tmp_path):
    config = Config(symbols=("ETHBTC",))
    venues = FakeVenues()
    store = Store(tmp_path/"ledger.sqlite3", "test", "test")
    engine = Engine(venues, store, config)
    run(engine.initialize(run(venues.account())))
    yield engine, venues, store
    store.close()


@pytest.mark.parametrize("partial,timeout", [(False, False), (True, False), (False, True), (True, True)])
def test_spot_actual_fills_preserve_reserves_and_never_resubmit(setup, partial, timeout):
    engine, venues, store = setup
    venues.partial, venues.timeout = partial, timeout
    req = {"symbol": "ETHBTC", "side": "BUY", "quantity": ".01", "price": ".03", "_market": spot_market()}
    run(engine.submit("spot", "one", deepcopy(req)))
    run(engine.submit("spot", "one", deepcopy(req)))
    assert len(venues.posts) == 1
    assert not store.pending()
    qty = D(".005") if partial else D(".01")
    assert D(store.get("wallet")["ETH"]) == qty
    assert D(store.get("wallet")["BTC"]) == D(".0012")-qty*D(".03")*D("1.001")
    assert store.get("reserve") == {"BTC": "0.0010", "ETH": "0.5"}
    engine.check_spot(run(venues.account()))


def test_unknown_order_blocks_new_intents_and_survives_restart(setup):
    engine, venues, store = setup
    venues.reject = True
    req = {"symbol": "ETHBTC", "side": "BUY", "quantity": ".01", "price": ".03", "_market": spot_market()}
    run(engine.submit("spot", "unknown", deepcopy(req)))
    assert len(store.pending()) == 1
    run(engine.submit("spot", "unknown", deepcopy(req)))
    assert len(venues.posts) == 1
    with pytest.raises(ValueError, match="Unresolved"):
        run(engine.submit("spot", "another", deepcopy(req)))


def test_delayed_fills_query_then_apply_once(setup):
    engine, venues, store = setup
    venues.spots["ETHBTC"].visibility = False
    run(engine.submit("spot", "delay", {"symbol":"ETHBTC", "side":"BUY", "quantity":".01", "price":".03", "_market":spot_market()}))
    assert store.pending() and D(store.get("wallet")["ETH"]) == 0
    venues.spots["ETHBTC"].visibility = True
    pending = store.pending()[0]
    assert run(engine.reconcile(pending))
    assert run(engine.reconcile(pending))
    assert D(store.get("wallet")["ETH"]) == D(".01")
    assert store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


@pytest.mark.parametrize("side,sign", [("BUY",1),("SELL",-1)])
def test_coinm_long_and_short_attach_stop_and_close_reduce_only(setup, side, sign):
    engine, venues, store = setup
    stop = "79000" if sign > 0 else "81000"
    req = {"symbol":"BTCUSD_PERP", "side":side, "quantity":"1", "price":"80000", "stop":stop, "reduce_only":False, "_market":spot_market()}
    run(engine.submit("coinm", "entry", req))
    run(engine.protect(run(venues.account())))
    assert D(store.get("coin_qty")) == sign
    protected = store.get("stop")
    assert venues.coin.stops[protected["id"]].side == ("SELL" if sign > 0 else "BUY")
    assert venues.coin.stops[protected["id"]].close_position
    run(engine.submit("coinm", "exit", {"symbol":"BTCUSD_PERP", "side":"SELL" if sign > 0 else "BUY",
        "quantity":"1", "price":"80000", "reduce_only":True, "_market":spot_market()}))
    run(engine.protect(run(venues.account())))
    assert venues.position == 0 and D(store.get("coin_qty")) == 0 and store.get("stop") is None


def test_stop_rejection_emergency_is_reduce_only_and_persisted(setup):
    engine, venues, store = setup
    run(engine.submit("coinm", "entry", {"symbol":"BTCUSD_PERP", "side":"BUY", "quantity":"1", "price":"80000",
        "stop":"79000", "reduce_only":False, "_market":spot_market()}))
    venues.coin.reject_stop = True
    with pytest.raises(ValueError):
        run(engine.protect(run(venues.account())))
    request = venues.posts[-1][2]
    assert request["emergency"] and request["reduce_only"] and request["side"] == "SELL"
    assert venues.position == 0
    assert store.get("halt") and store.pending()


def test_external_balance_drift_is_not_adopted(setup):
    engine, venues, store = setup
    venues.assets["BTC"] += D(".0001")
    with pytest.raises(ValueError, match="drift"):
        engine.check_spot(run(venues.account()))


def test_mismatched_fill_rolls_back_wallet(setup):
    engine, venues, store = setup
    venues.spots["ETHBTC"].visibility = False
    run(engine.submit("spot", "bad", {"symbol":"ETHBTC", "side":"BUY", "quantity":".01", "price":".03", "_market":spot_market()}))
    venues.spots["ETHBTC"].visibility = True
    venues.spots["ETHBTC"].rows[1][0]["isBuyer"] = False
    with pytest.raises(ValueError):
        run(engine.reconcile(store.pending()[0]))
    assert D(store.get("wallet")["ETH"]) == 0


def test_alt_filters_apply_to_alt_units_not_btc(setup):
    engine, venues, store = setup
    market = spot_market()
    market["filters"].append({"filterType":"MAX_POSITION", "maxPosition":".50001"})
    with pytest.raises(ValueError, match="MAX_POSITION"):
        run(engine.spot_trade("ETHBTC", D(".5"), market, D(".001"), run(venues.account()), "max"))
    assert not venues.posts


def test_disabled_gateway_rejects_posts_and_other_symbols():
    gateway = SpotGateway("SOLBTC", "key", "secret")
    with pytest.raises(GatewayError):
        run(gateway.place_order("bsg_test", "BUY", D(1), limit_price=D(".01")))
    with pytest.raises(GatewayError):
        gateway._validate_request("GET", "/api/v3/ticker/price", {"symbol":"BTCUSDT"}, False)
    with pytest.raises(ValueError):
        SpotGateway("BTCUSDT")


def test_futures_guard_forbids_transfers_modes_and_foreign_orders():
    venues = Venues(Config())
    with pytest.raises(ValueError):
        venues.guard("POST", "/dapi/v1/order", {"symbol":"BTCUSD_PERP", "newClientOrderId":"bsg_a"})
    venues.allow_orders = True
    for path in ("/sapi/v1/asset/transfer", "/dapi/v1/positionSide/dual", "/dapi/v1/leverage"):
        with pytest.raises(ValueError):
            venues.guard("POST", path, {})
    with pytest.raises(ValueError):
        venues.guard("DELETE", "/dapi/v1/algoOrder", {"clientAlgoId":"foreign"})


def test_closed_candles_do_not_use_live_bar_and_detect_missing_data():
    period = 86_400_000
    bars = candles(period, 61)
    now = int(time.time()*1000)
    assert len(closed(bars, now, period, 61)) == 61
    bad = deepcopy(bars)
    bad[-1][0] -= period
    with pytest.raises(ValueError):
        closed(bad, now, period, 61)


def test_alt_rotation_prefers_btc_outperformance_and_cash_when_all_negative():
    a, b = spot_market(), spot_market()
    b["klines"] = candles(86_400_000, 61, 2)
    assert alt_signal({"ETHBTC":a,"SOLBTC":b})["symbol"] == "SOLBTC"
    a["klines"] = candles(86_400_000,61,-1)
    assert alt_signal({"ETHBTC":a})["symbol"] is None


@pytest.mark.parametrize("slope,direction", [(1,1),(-1,-1),(0,0)])
def test_coinm_both_directions(slope, direction):
    market = {"klines":candles(14_400_000,200,slope),"server_time_ms":int(time.time()*1000)}
    assert coinm_signal(market)["direction"] == direction


def test_configuration_cannot_silently_increase_risk():
    with pytest.raises(ValueError):
        Config(risk_fraction=".5")
    with pytest.raises(ValueError):
        Config(spot_btc="NaN")


def test_allocation_review_reports_material_drift_without_changing_budgets():
    from btc_portfolio.engine import allocation_snapshot
    config = Config()
    initial = allocation_snapshot(config, D(".0012"), D(".0018"))
    assert initial["spot_weight_pct"] == "40.0"
    assert initial["rebalance_review"] is False
    grown = allocation_snapshot(config, D(".0016"), D(".0018"))
    assert grown["rebalance_review"] is True
    assert D(grown["spot_excess_btc"]) == D(".00024")
    assert config.spot_btc == "0.0012" and config.coinm_btc == "0.0018"


def test_ledger_binding_rejects_strategy_or_budget_change(tmp_path):
    path = tmp_path/"ledger.sqlite3"
    Store(path,"one","live").close()
    with pytest.raises(ValueError):
        Store(path,"two","live")


@pytest.mark.parametrize("direction", [1,-1])
def test_inverse_quantity_respects_exact_stop_loss_budget(direction):
    from pathlib import Path
    from binance_coinm_v1.exchange.contract import resolve_contract
    data = json.loads(Path("binance_coinm_v1/tests/fixtures/exchange_info_coinm.json").read_text(encoding="utf-8"))
    spec = resolve_contract(data,"BTCUSD_PERP")
    config = Config()
    market = {"spec":spec,"ask":"80000","bid":"80000","mark":"80000"}
    signal = {"direction":direction,"stop_fraction":".01"}
    plan = coin_plan(config,market,signal,D(".0005"),D(".0018"),config.total)
    assert plan["status"] == "READY"
    assert D(plan["quantity"]) >= 1
    assert D(plan["risk_btc"]) <= config.total*D(config.risk_fraction)
    plan = coin_plan(config,market,{**signal,"stop_fraction":".08"},D(".0005"),D(".0018"),config.total)
    assert plan["status"] == "SKIP"
    assert plan["reason"] == "minimum_contract_exceeds_risk_or_budget"


def test_empty_futures_wallet_is_a_funding_blocker(setup):
    engine, venues, store = setup
    account = run(venues.account())
    account["coin"].assets["BTC"] = AssetBalance("BTC",0,0,0,0)
    assert "coinm_btc_allocation_not_funded_transfer_required" in Engine(venues,None,Config()).readiness(account)


def test_new_day_tick_executes_alt_then_holds_without_rebuy(setup, monkeypatch):
    engine, venues, store = setup
    from pathlib import Path
    from binance_coinm_v1.exchange.contract import resolve_contract
    spec = resolve_contract(json.loads(Path("binance_coinm_v1/tests/fixtures/exchange_info_coinm.json").read_text()),"BTCUSD_PERP")
    async def markets():
        return {"spot":{"ETHBTC":spot_market()}, "coinm":{"spec":spec,"ask":"80000","bid":"80000","mark":"80000",
            "received_at_ms":int(time.time()*1000),"server_time_ms":int(time.time()*1000),"klines":candles(14_400_000,200,0)}}
    async def fees():
        return {"spot":{"ETHBTC":D(".001")},"coinm":D(".0005")}
    venues.markets, venues.fees = markets, fees
    first = run(engine.tick())
    assert first["action"]["symbol"] == "ETHBTC" and first["action"]["side"] == "BUY"
    run(engine.tick())
    assert len(venues.posts) == 1
    assert not store.pending()


def test_spot_sell_returns_btc_and_preserves_existing_alt_reserve(setup):
    engine, venues, store = setup
    run(engine.submit("spot", "buy", {"symbol":"ETHBTC", "side":"BUY", "quantity":".01", "price":".03", "_market":spot_market()}))
    run(engine.submit("spot", "sell", {"symbol":"ETHBTC", "side":"SELL", "quantity":".01", "price":".04", "_market":spot_market()}))
    assert D(store.get("wallet")["BTC"]) > D(".0012")
    assert D(store.get("wallet")["ETH"]) == 0
    assert venues.assets["ETH"] == D(".5")
    engine.check_spot(run(venues.account()))


def test_pending_intent_and_wallet_survive_database_reopen(tmp_path):
    path = tmp_path/"ledger.sqlite3"
    store = Store(path,"identity","live")
    store.put("wallet", {"BTC":".0012","SOL":"0"})
    request = {"symbol":"SOLBTC","side":"BUY","quantity":"1","price":".002"}
    ident, is_new = store.intent("spot","day1",request)
    assert is_new
    store.close()
    store = Store(path,"identity","live")
    assert store.intent("spot","day1",request) == (ident,False)
    assert store.get("wallet")["BTC"] == ".0012"
    assert len(store.pending()) == 1
    store.close()


def test_flat_position_clears_old_stop_price_before_next_entry(setup):
    engine, venues, store = setup
    store.put("position_stop", "100000")
    run(engine.protect(run(venues.account())))
    assert store.get("position_stop") is None
