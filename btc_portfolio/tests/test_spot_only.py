"""Spot-only aggressive mode: the COIN-M account belongs to the user and the bot never touches it."""
import asyncio
from decimal import Decimal as D

import pytest

from btc_portfolio.code_update import rebind_blockers
from btc_portfolio.config import Config
from btc_portfolio.engine import Engine
from btc_portfolio.runtime import prepare
from btc_portfolio.store import Store
from btc_portfolio.venues import Venues
from btc_portfolio.tests.test_aggressive import coin_market
from btc_portfolio.tests.test_portfolio import FakeVenues, spot_market

SPOT_ONLY = dict(strategy_mode="aggressive", spot_fraction="1", coinm_managed=False)


class NoCoin:
    """Any private COIN-M call fails the test."""
    def __getattr__(self, name):
        raise AssertionError("COIN-M touched: " + name)


class SpotOnlyVenues(FakeVenues):
    def __init__(self):
        super().__init__()
        self.coin = NoCoin()

    async def account(self):
        return {"spot": {"uid": 123, "canTrade": True, "balances": [
                    {"asset": k, "free": str(v), "locked": "0"} for k, v in self.assets.items()]},
                "permissions": {"enableSpotAndMarginTrading": True}, "spot_bnb_burn": False, "spot_orders": []}

    async def markets(self):
        return {"spot": {"ETHBTC": spot_market()}, "coinm": coin_market()}

    async def fees(self):
        return {"spot": {"ETHBTC": D(".001")}, "coinm": None}

    async def submit(self, ident, venue, req):
        assert venue == "spot", "COIN-M order submitted"
        await super().submit(ident, venue, req)


def registered(tmp_path, identity="unit", mode="test"):
    """A ledger as the live bot left it: owned spot wallet, flat COIN-M, no stop."""
    store = Store(tmp_path/"ledger.sqlite3", identity, mode)
    full = FakeVenues()
    account = asyncio.run(full.account())
    account["spot_bnb_burn"] = False
    asyncio.run(Engine(full, store, Config(strategy_mode="aggressive")).initialize(account))
    store.put("aggressive_initial_equity", ".003")
    return store


def test_spot_only_needs_all_capital_in_spot_and_no_futures_features():
    assert not Config(**SPOT_ONLY).coinm_managed
    for bad in (dict(SPOT_ONLY, spot_fraction=".5"), dict(SPOT_ONLY, strategy_mode="swing"),
                dict(SPOT_ONLY, timing_prediction_path="/var/lib/btc-ledger/prediction.json"),
                dict(SPOT_ONLY, rebalance_mode="auto"), dict(SPOT_ONLY, coinm_managed="false")):
        with pytest.raises(ValueError):
            Config(**bad)
    assert Config(**SPOT_ONLY).identity() != Config(**dict(SPOT_ONLY, coinm_managed=True)).identity()


def test_tick_rotates_alts_without_any_coinm_call(tmp_path):
    store = registered(tmp_path)
    try:
        venues = SpotOnlyVenues()
        result = asyncio.run(Engine(venues, store, Config(**SPOT_ONLY)).tick())
        assert result["status"] == "READY" and result["action"]["side"] == "BUY"
        assert [post[1] for post in venues.posts] == ["spot"]
        assert D(result["equity_btc"]) == D(".0012")             # spot wallet only
        assert result["allocation"]["coinm_equity_btc"] == "0" and result["coinm_signal"] is None
        assert store.get("spot_only_initial_equity") == "0.0012"
        assert store.get("aggressive_initial_equity") == ".003"   # the old baseline is kept, not reused
    finally:
        store.close()


@pytest.mark.parametrize("key,value", [("coin_qty", "3"), ("stop", {"id": "bsg_x"}), ("position_stop", "74704.6")])
def test_ledger_that_still_owns_coinm_blocks_spot_only(tmp_path, key, value):
    store = registered(tmp_path)
    try:
        store.put(key, value)
        venues = SpotOnlyVenues()
        result = asyncio.run(Engine(venues, store, Config(**SPOT_ONLY)).tick())
        assert result == {"status": "BLOCKED", "reasons": ["ledger_still_owns_coinm_position"]}
        assert venues.posts == []
    finally:
        store.close()


def test_pending_coinm_intent_is_not_reconciled_through_the_users_account(tmp_path):
    store = registered(tmp_path)
    try:
        store.intent("coinm", "old-close", {"symbol": "BTCUSD_PERP", "side": "SELL", "quantity": "3",
                                            "price": "84000", "reduce_only": True})
        result = asyncio.run(Engine(SpotOnlyVenues(), store, Config(**SPOT_ONLY)).tick())
        assert result == {"status": "BLOCKED", "reason": "ledger_still_owns_coinm_order"}
    finally:
        store.close()


def test_venues_skip_private_coinm_reads_and_refuse_coinm_orders():
    venues = Venues(Config(**SPOT_ONLY), None, allow_orders=True)
    real, venues.coin = venues.coin, NoCoin()
    for gateway in venues.spots.values():
        async def value(*args, _v=None, **kwargs):
            return _v
        gateway.account = lambda: value(_v={"uid": 1})
        gateway.permissions = lambda: value(_v={"enableSpotAndMarginTrading": True})
        gateway.open_orders = lambda all_symbols=False: value(_v=[])
        gateway.bnb_burn_status = lambda: value(_v=False)
        gateway.commission_rate = lambda: value(_v=D(".001"))
    account = asyncio.run(venues.account())
    assert set(account) == {"spot", "permissions", "spot_bnb_burn", "spot_orders"}
    assert asyncio.run(venues.fees())["coinm"] is None
    for method, path, params in (("POST", "/dapi/v1/order", {"newClientOrderId": "bsg_a", "symbol": "BTCUSD_PERP"}),
                                 ("DELETE", "/dapi/v1/algoOrder", {"clientAlgoId": "bsg_b"})):
        with pytest.raises(ValueError, match="managed by the user"):
            venues.guard(method, path, params)
    venues.coin = real
    asyncio.run(venues.close())


def test_prepare_reports_manual_coinm_without_reading_it(tmp_path):
    config = Config(**SPOT_ONLY)
    registered(tmp_path, config.identity(), "live").close()
    result = asyncio.run(prepare(SpotOnlyVenues(), config, tmp_path))
    assert result["coinm"] == "manual" and result["coinm_signal"] is None
    assert "coinm_leverage" not in result and D(result["spot_equity_btc"]) == D(".0012")
    assert not {"ledger_still_owns_coinm_position", "portfolio_ledger_binding_changed"} & set(result["blockers"])


def test_rebind_to_spot_only_needs_a_ledger_that_owns_nothing_on_coinm():
    state = {"binding": {"identity": "old", "mode": "live"}, "coin_qty": "0", "stop": None}
    spot_account = {"spot": {}, "permissions": {}, "spot_orders": []}
    ready = {"blockers": ["portfolio_ledger_binding_changed"]}
    assert rebind_blockers(state, spot_account, ready, True) == []
    assert rebind_blockers({**state, "coin_qty": "3"}, spot_account, ready, True) == ["ledger_still_owns_coinm_position"]
    assert rebind_blockers({**state, "stop": {"id": "bsg_x"}}, spot_account, ready, True) == ["ledger_still_owns_coinm_position"]
