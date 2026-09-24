import asyncio
from copy import deepcopy
from decimal import Decimal as D
import json

import pytest

from btc_spot import paper
from btc_spot.paper import PaperGateway, PaperOrderRejected


NOW = 1_800_000_000_000


def run(awaitable):
    return asyncio.run(awaitable)


class PublicOnly:
    has_credentials = False
    allow_orders = False

    def __init__(self):
        self.calls = []
        self.data = {"symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT",
                     "status": "TRADING", "spot_allowed": True, "server_time_ms": NOW,
                     "received_at_ms": NOW, "bid": "80000", "ask": "80001",
                     "reference_price": "80000", "bid_qty": "1", "ask_qty": "1", "klines": [],
                     "filters": [{"filterType": "PRICE_FILTER", "minPrice": ".01", "maxPrice": "10000000", "tickSize": ".01"},
                                 {"filterType": "LOT_SIZE", "minQty": ".00001", "maxQty": "9000", "stepSize": ".00001"},
                                 {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "9000", "stepSize": "0"},
                                 {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True,
                                  "maxNotional": "9000000", "applyMaxToMarket": False, "avgPriceMins": 5}]}

    async def market(self):
        self.calls.append("market")
        return self.data

    async def close(self):
        self.calls.append("close")

    def __getattr__(self, name):
        raise AssertionError("Private/public order method must never be accessed: " + name)


@pytest.fixture(autouse=True)
def stable_clock(monkeypatch):
    monkeypatch.setattr(paper, "_now_ms", lambda: NOW)


@pytest.fixture
def exchange(tmp_path):
    value = PaperGateway(PublicOnly(), tmp_path / "exchange.json")
    yield value
    run(value.close())


def wallet(gateway):
    return {row["asset"]: D(row["free"]) for row in run(gateway.account())["balances"]}


def test_initial_paper_wallet_has_no_reserve_or_quote_and_no_private_calls(exchange):
    assert wallet(exchange) == {"BTC": D(".003"), "USDT": 0}
    assert run(exchange.open_orders()) == []
    assert run(exchange.get_order("absent")) is None
    assert run(exchange.trades(123)) == []
    assert exchange.public_gateway.calls == []


def test_sell_then_buy_accounts_for_spread_slippage_and_received_asset_commissions(exchange):
    sell = run(exchange.place_order("bsg_sell", "SELL", ".001"))
    sell_price = D("80000") * D(".9997")
    assert sell["status"] == "FILLED" and sell["executedQty"] == "0.001"
    assert D(sell["cummulativeQuoteQty"]) == D(".001") * sell_price
    assert sell["fills"][0]["commissionAsset"] == "USDT"
    assert wallet(exchange) == {"BTC": D(".002"), "USDT": D(".001") * sell_price * D(".999")}
    buy = run(exchange.place_order("bsg_buy", "BUY", ".0009"))
    buy_price = D("80001") * D("1.0003")
    assert buy["fills"][0]["commissionAsset"] == "BTC"
    assert D(buy["fills"][0]["commission"]) == D(".0000009")
    assert wallet(exchange) == {"BTC": D(".0028991"),
                                "USDT": D(".001") * sell_price * D(".999") - D(".0009") * buy_price}
    trade = run(exchange.trades(buy["orderId"]))[0]
    assert D(trade["qty"]) == D(buy["executedQty"])
    assert D(trade["quoteQty"]) == D(buy["cummulativeQuoteQty"])
    assert trade["commissionAsset"] == "BTC" and trade["isBuyer"] is True
    assert exchange.public_gateway.calls == ["market", "market"]


def test_restart_recovers_complete_orders_and_wallet_without_replaying_fills(tmp_path):
    path = tmp_path / "exchange.json"
    first = PaperGateway(PublicOnly(), path)
    order = run(first.place_order("bsg_restart", "SELL", ".001"))
    before = wallet(first)
    run(first.close())
    second = PaperGateway(PublicOnly(), path)
    try:
        assert wallet(second) == before
        assert run(second.get_order("bsg_restart")) == order
        assert run(second.trades(order["orderId"]))[0]["id"] == 1
        assert second.public_gateway.calls == []
        with pytest.raises(PaperOrderRejected, match="Duplicate"):
            run(second.place_order("bsg_restart", "SELL", ".001"))
        assert wallet(second) == before
    finally:
        run(second.close())


def test_duplicate_id_is_rejected_before_another_market_fetch(exchange):
    run(exchange.place_order("bsg_same", "SELL", ".001"))
    before = wallet(exchange)
    with pytest.raises(PaperOrderRejected, match="Duplicate"):
        run(exchange.place_order("bsg_same", "SELL", ".001"))
    assert wallet(exchange) == before
    assert exchange.public_gateway.calls == ["market"]


def test_concurrent_duplicate_requests_produce_exactly_one_durable_fill(exchange):
    async def duplicate():
        return await asyncio.gather(exchange.place_order("bsg_race", "SELL", ".001"),
                                    exchange.place_order("bsg_race", "SELL", ".001"), return_exceptions=True)
    answers = run(duplicate())
    assert sum(isinstance(answer, PaperOrderRejected) for answer in answers) == 1
    assert wallet(exchange)["BTC"] == D(".002")
    assert len(json.loads(exchange.state_path.read_text())["trades"]) == 1


@pytest.mark.parametrize("change", [{"initial_btc": D(".0033")}, {"fee_rate": D(".002")}, {"slippage_bps": D("6")}])
def test_restart_rejects_changed_budget_or_cost_fingerprint(tmp_path, change):
    path = tmp_path / "exchange.json"
    first = PaperGateway(PublicOnly(), path)
    run(first.close())
    with pytest.raises(ValueError, match="fingerprint"):
        PaperGateway(PublicOnly(), path, **change)
    unchanged = PaperGateway(PublicOnly(), path)
    run(unchanged.close())


@pytest.mark.parametrize("quantity", ["0", ".00001", ".000061", "-1", "NaN", "Infinity"])
def test_invalid_quantity_never_changes_wallet(exchange, quantity):
    before = exchange.state_path.read_bytes()
    with pytest.raises(ValueError):
        run(exchange.place_order("bsg_invalid", "SELL", quantity))
    assert exchange.state_path.read_bytes() == before


@pytest.mark.parametrize("side,quantity", [("BUY", ".001"), ("SELL", ".004")])
def test_insufficient_balances_do_not_borrow(exchange, side, quantity):
    with pytest.raises(PaperOrderRejected, match="Insufficient"):
        run(exchange.place_order("bsg_empty", side, quantity))
    assert wallet(exchange) == {"BTC": D(".003"), "USDT": 0}


@pytest.mark.parametrize("field,age", [("received_at_ms", 30001), ("server_time_ms", 30001),
                                       ("received_at_ms", -5001), ("server_time_ms", -5001)])
def test_old_or_future_quotes_are_never_used_for_fills(exchange, field, age):
    exchange.public_gateway.data[field] = NOW - age
    with pytest.raises(PaperOrderRejected, match="stale"):
        run(exchange.place_order("bsg_stale", "SELL", ".001"))
    assert run(exchange.get_order("bsg_stale")) is None


def test_order_rechecks_current_book_instead_of_using_mutated_or_old_signal_quote(exchange):
    snapshot = run(exchange.market())
    snapshot["bid"] = "999999"
    exchange.public_gateway.data["bid"] = "70000"
    exchange.public_gateway.data["ask"] = "70001"
    order = run(exchange.place_order("bsg_fresh", "SELL", ".001"))
    assert D(order["fills"][0]["price"]) == D("70000") * D(".9997")
    assert exchange.public_gateway.data["bid"] == "70000"


def test_fill_cannot_exceed_observed_best_book_quantity(exchange):
    exchange.public_gateway.data["bid_qty"] = ".0001"
    with pytest.raises(PaperOrderRejected, match="best-book"):
        run(exchange.place_order("bsg_depth", "SELL", ".001"))


def test_raw_order_and_trade_responses_are_copies(exchange):
    response = run(exchange.place_order("bsg_copy", "SELL", ".001"))
    response["fills"][0]["qty"] = "1"
    trades = run(exchange.trades(1))
    trades[0]["qty"] = "1"
    assert D(run(exchange.get_order("bsg_copy"))["fills"][0]["qty"]) == D(".001")
    assert D(run(exchange.trades(1))[0]["qty"]) == D(".001")


def test_order_state_exists_on_disk_before_returning_fill(exchange, monkeypatch):
    original = paper._atomic_write
    observed = []
    def spy(path, value):
        original(path, value)
        observed.append(json.loads(path.read_text())["orders"]["bsg_durable"]["status"])
    monkeypatch.setattr(paper, "_atomic_write", spy)
    response = run(exchange.place_order("bsg_durable", "SELL", ".001"))
    assert observed == ["FILLED"]
    assert json.loads(exchange.state_path.read_text())["orders"]["bsg_durable"] == response


def test_lost_persistence_acknowledgement_forces_restart_and_recovers_once(tmp_path, monkeypatch):
    path = tmp_path / "exchange.json"
    first = PaperGateway(PublicOnly(), path)
    original = paper._atomic_write
    def persist_then_fail(path, value):
        original(path, value)
        raise OSError("Simulated acknowledgement failure")
    monkeypatch.setattr(paper, "_atomic_write", persist_then_fail)
    with pytest.raises(OSError):
        run(first.place_order("bsg_lost", "SELL", ".001"))
    with pytest.raises(ValueError, match="uncertain"):
        run(first.place_order("bsg_another", "SELL", ".001"))
    run(first.close())
    monkeypatch.setattr(paper, "_atomic_write", original)
    second = PaperGateway(PublicOnly(), path)
    try:
        assert run(second.get_order("bsg_lost"))["status"] == "FILLED"
        assert wallet(second)["BTC"] == D(".002")
        assert len(run(second.trades(1))) == 1
    finally:
        run(second.close())


def test_persistence_failure_before_commit_preserves_original_wallet(tmp_path, monkeypatch):
    path = tmp_path / "exchange.json"
    first = PaperGateway(PublicOnly(), path)
    original_bytes = path.read_bytes()
    original = paper._atomic_write
    def fail(path, value):
        raise OSError("Simulated disk failure")
    monkeypatch.setattr(paper, "_atomic_write", fail)
    with pytest.raises(OSError):
        run(first.place_order("bsg_disk", "SELL", ".001"))
    assert path.read_bytes() == original_bytes
    run(first.close())
    monkeypatch.setattr(paper, "_atomic_write", original)
    second = PaperGateway(PublicOnly(), path)
    try:
        assert wallet(second)["BTC"] == D(".003")
        assert run(second.get_order("bsg_disk")) is None
    finally:
        run(second.close())


def test_another_writer_cannot_open_same_paper_state(exchange):
    with pytest.raises(ValueError, match="writer"):
        PaperGateway(PublicOnly(), exchange.state_path)


@pytest.mark.parametrize("credentials,allow", [(True, False), (False, True), (True, True)])
def test_private_or_order_enabled_gateway_rejected_without_forwarding(tmp_path, credentials, allow):
    public = PublicOnly()
    public.has_credentials, public.allow_orders = credentials, allow
    with pytest.raises(ValueError, match="keyless"):
        PaperGateway(public, tmp_path / "exchange.json")
    assert public.calls == []


@pytest.mark.parametrize("tamper", ["balance", "fee", "json"])
def test_corrupt_persisted_state_fails_closed_instead_of_resetting(tmp_path, tamper):
    path = tmp_path / "exchange.json"
    first = PaperGateway(PublicOnly(), path)
    run(first.place_order("bsg_check", "SELL", ".001"))
    run(first.close())
    state = json.loads(path.read_text())
    if tamper == "balance":
        state["balances"]["BTC"] = ".003"
    elif tamper == "fee":
        state["trades"][0]["commission"] = "0"
    path.write_text("{bad" if tamper == "json" else json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError):
        PaperGateway(PublicOnly(), path)


def test_close_is_idempotent_and_releases_writer(exchange):
    run(exchange.close())
    run(exchange.close())
    assert exchange.public_gateway.calls == ["close"]
    with pytest.raises(ValueError, match="closed"):
        run(exchange.account())
    replacement = PaperGateway(PublicOnly(), exchange.state_path)
    run(replacement.close())


def test_ioc_sell_expires_without_a_fill_when_adverse_price_crosses_bound(exchange):
    before = wallet(exchange)
    order = run(exchange.place_order("bsg_expired", "SELL", ".001", limit_price="80000"))
    assert order["type"] == "LIMIT" and order["timeInForce"] == "IOC"
    assert order["price"] == "80000" and order["status"] == "EXPIRED"
    assert order["executedQty"] == order["cummulativeQuoteQty"] == "0"
    assert order["fills"] == [] and run(exchange.trades(order["orderId"])) == []
    assert wallet(exchange) == before
    assert run(exchange.get_order("bsg_expired")) == order
    assert run(exchange.open_orders(all_symbols=True)) == []


def test_ioc_fill_can_equal_sell_limit_and_preserves_received_asset_fee(exchange):
    order = run(exchange.place_order("bsg_exact", "SELL", ".001", limit_price="79976.00"))
    assert order["status"] == "FILLED"
    assert D(order["fills"][0]["price"]) == D(order["price"])
    assert order["fills"][0]["commissionAsset"] == "USDT"


def test_ioc_buy_never_exceeds_limit_even_by_fraction_of_a_tick(exchange):
    run(exchange.place_order("bsg_fund", "SELL", ".002"))
    before = wallet(exchange)
    # Observed ask*(1+3bps)=80025.0003, marginally beyond the lower limit.
    expired = run(exchange.place_order("bsg_low", "BUY", ".001", limit_price="80025.00"))
    assert expired["status"] == "EXPIRED" and wallet(exchange) == before
    filled = run(exchange.place_order("bsg_high", "BUY", ".001", limit_price="80025.01"))
    assert filled["status"] == "FILLED"
    assert D(filled["fills"][0]["price"]) <= D(filled["price"])
    assert filled["fills"][0]["commissionAsset"] == "BTC"
    assert D(filled["cummulativeQuoteQty"]) <= D(filled["origQty"]) * D(filled["price"])


def test_expired_ioc_persists_and_does_not_consume_a_trade_id_on_restart(tmp_path):
    path = tmp_path / "exchange.json"
    first = PaperGateway(PublicOnly(), path)
    expired = run(first.place_order("bsg_exp", "SELL", ".001", limit_price="80000"))
    filled = run(first.place_order("bsg_done", "SELL", ".001", limit_price="79976"))
    assert expired["orderId"] == 1 and filled["orderId"] == 2
    assert filled["fills"][0]["tradeId"] == 1
    run(first.close())
    second = PaperGateway(PublicOnly(), path)
    try:
        assert run(second.get_order("bsg_exp"))["status"] == "EXPIRED"
        assert run(second.trades(1)) == []
        assert run(second.trades(2))[0]["id"] == 1
        assert wallet(second)["BTC"] == D(".002")
        with pytest.raises(PaperOrderRejected, match="Duplicate"):
            run(second.place_order("bsg_exp", "SELL", ".001", limit_price="79976"))
    finally:
        run(second.close())


@pytest.mark.parametrize("limit", ["0", "-1", "80000.001", "NaN", "Infinity", "10000001"])
def test_ioc_rejects_invalid_price_grid_or_bound_without_mutating_state(exchange, limit):
    before = exchange.state_path.read_bytes()
    with pytest.raises(ValueError):
        run(exchange.place_order("bsg_price", "SELL", ".001", limit_price=limit))
    assert exchange.state_path.read_bytes() == before


def test_ioc_uses_limit_notional_and_all_limit_bounds(exchange):
    notional = next(row for row in exchange.public_gateway.data["filters"] if row["filterType"] == "NOTIONAL")
    notional["maxNotional"] = "60"
    assert notional["applyMaxToMarket"] is False
    with pytest.raises(PaperOrderRejected, match="filters"):
        run(exchange.place_order("bsg_max", "SELL", ".001", limit_price="80000"))


def test_ioc_uses_lot_size_instead_of_market_lot_size(exchange):
    market_lot = next(row for row in exchange.public_gateway.data["filters"] if row["filterType"] == "MARKET_LOT_SIZE")
    market_lot["maxQty"] = ".0001"
    order = run(exchange.place_order("bsg_lot", "SELL", ".001", limit_price="79976"))
    assert order["status"] == "FILLED"


def test_ioc_with_insufficient_book_depth_expires_instead_of_inventing_full_fill(exchange):
    exchange.public_gateway.data["bid_qty"] = ".0001"
    order = run(exchange.place_order("bsg_depth_ioc", "SELL", ".001", limit_price="79976"))
    assert order["status"] == "EXPIRED"
    assert wallet(exchange)["BTC"] == D(".003")


def test_ioc_requires_enough_quote_at_its_limit_even_if_observed_price_is_affordable(exchange):
    run(exchange.place_order("bsg_cash", "SELL", ".001"))
    # .0009*80025.0003 is affordable, but .0009*90000 exceeds available USDT.
    with pytest.raises(PaperOrderRejected, match="Insufficient"):
        run(exchange.place_order("bsg_bound", "BUY", ".0009", limit_price="90000"))
