import asyncio
from decimal import Decimal as D
import math
import hashlib
import json
import time
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from btc_lab import aggressive_backtest as replay
from btc_portfolio.aggressive import (coin_order_plan, contract_count, execute,
    liquidation_estimate, preview, rebalance_amount, stop_and_liquidation, target_leverage,
    validate_actual_position, volatility)
from btc_portfolio.signals import alt_signal, coinm_signal
from btc_portfolio.config import Config
from btc_portfolio.engine import Engine
from btc_portfolio.store import Store
from btc_portfolio.transfer import rebalance_live, transfer_history
from btc_portfolio.migration import plan_snapshot, snapshot
from btc_portfolio.portfolio_transfer import plan as stopped_transfer_plan
from btc_portfolio.tests.test_portfolio import FakeVenues, candles, spot_market


class Spec:
    contract_size = D(100)
    max_qty = D(100000)
    maint_margin_pct = D("2.5")

    def round_price(self, price, mode):
        from decimal import ROUND_CEILING, ROUND_FLOOR
        return (D(price)/D("0.1")).to_integral_value(rounding=ROUND_CEILING if mode == "up" else ROUND_FLOOR)*D("0.1")

    def round_price_away(self, price, direction, is_stop):
        return self.round_price(price, "down" if direction > 0 else "up")

    def check_price(self, price):
        return True, ""

    def check_qty(self, qty, market=False):
        return qty >= 1 and qty == int(qty), ""


def coin_market(closes=None):
    period = 14_400_000
    end = int(time.time()*1000)//period*period
    closes = closes if closes is not None else [84000*(1+0.0001*i) for i in range(210)]
    rows = [[end-(len(closes)-i)*period, str(x), str(x*1.01), str(x*.99), str(x), "1",
             end-(len(closes)-i-1)*period-1] for i, x in enumerate(closes)]
    return {"klines": rows, "server_time_ms": end+1000, "received_at_ms": int(time.time()*1000),
            "bid": "84000", "ask": "84001", "mark": "84000", "spec": Spec()}


def test_volatility_leverage_and_contracts_match_replay_formulas():
    closes = 84000*np.exp(np.cumsum(np.random.default_rng(12).normal(0, .011, 210)))
    market = coin_market(closes)
    sigma = volatility(market)
    expected = np.std(np.diff(np.log(closes[-181:])), ddof=1)
    assert float(sigma) == pytest.approx(expected, rel=1e-12)
    cfg = Config(strategy_mode="aggressive")
    lev = target_leverage(cfg, sigma)
    assert float(lev) == pytest.approx(float(np.clip(2*.0113579/expected, 1, 3)))
    assert contract_count(lev, D(".0015"), 84000, 100) == math.floor(float(lev)*.0015*84000/100)
    assert float(liquidation_estimate(84000, 1, D(".025"))) == pytest.approx(replay.liquidation_price(84000, 1, .025))
    assert float(liquidation_estimate(84000, -1, D(".025"))) == pytest.approx(replay.liquidation_price(84000, -1, .025))


def test_completed_bar_alt_and_coin_directions_match_replay():
    rng = np.random.default_rng(42)
    prices = 84000*np.exp(np.cumsum(rng.normal(0, .009, 210)))
    market = coin_market(prices)
    comparison = np.r_[prices[-200:], prices[-1]]
    fast = replay.window_ema(comparison, 20)[200]
    slow = replay.window_ema(comparison, 80)[200]
    assert coinm_signal(market)["direction"] == (1 if fast > slow else -1 if fast < slow else 0)

    period = 86_400_000
    end = int(time.time()*1000)//period*period
    histories = {}
    markets = {}
    for index, symbol in enumerate(("ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC")):
        closes = .03*np.exp(np.cumsum(rng.normal(.0005*(index-1), .025, 75)))
        histories[symbol] = closes
        markets[symbol] = {"server_time_ms": end+1000, "klines": [
            [end-(len(closes)-i)*period, str(x), str(x*1.01), str(x*.99), str(x), "1",
             end-(len(closes)-i-1)*period-1] for i, x in enumerate(closes)]}
    synthetic = SimpleNamespace(dclose=histories)
    expected, scores = replay.Data.alt_winner(synthetic, 75)
    observed = alt_signal(markets)
    assert observed["symbol"] == expected
    for symbol in scores:
        assert float(observed["scores"][symbol]) == pytest.approx(scores[symbol], rel=1e-12)


def test_rebalance_sign_matches_replay_monthly_transfer():
    assert rebalance_amount(".0012", ".0018", ".5") == D("-.0003")
    assert rebalance_amount(".0018", ".0012", ".5") == D(".0003")
    assert rebalance_amount(".00155", ".00145", ".5") == 0


def test_unregistered_local_prepare_does_not_propose_transfer_from_whole_account():
    spot = {}
    for symbol in ("ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC"):
        market = deepcopy(spot_market())
        market["symbol"] = symbol
        market["base_asset"] = symbol[:-3]
        spot[symbol] = market
    account = asyncio.run(FakeVenues().account())
    view = preview(Config(strategy_mode="aggressive"), {"spot": spot, "coinm": coin_market()}, account)
    assert view["allocation_source"] == "whole_spot_account_estimate"
    assert view["spot_to_coinm_btc"] is None and view["coinm_to_spot_btc"] is None
    assert view["target_contracts_after_rebalance"] is None


def test_three_times_cap_and_stop_liquidation_gap():
    cfg = Config(strategy_mode="aggressive", leverage_mode="fixed", leverage_base="3")
    market = coin_market()
    plan = coin_order_plan(cfg, market, 1, D(".0015"), D(".0015"), D(".0005"))
    assert plan["status"] == "READY"
    assert int(plan["quantity"])*100/D(plan["price"])/D(".0015") <= 3
    assert stop_and_liquidation(market, 1, D("84000"), D(".12"))["safe"]
    assert not stop_and_liquidation(market, 1, D("84000"), D(".20"))["safe"]
    assert coin_order_plan(cfg, market, 1, D(".0002"), D(".0002"), D(".0005"))["status"] == "SKIP"


def test_bnb_discount_is_a_readiness_blocker(tmp_path):
    cfg = Config(strategy_mode="aggressive")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        engine = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = True
        assert "disable_spot_bnb_fee_payment_before_aggressive_trading" in engine.readiness(account)
    finally:
        s.close()


def test_actual_liquidation_violation_requests_reduce_only_and_halts(tmp_path):
    cfg = Config(strategy_mode="aggressive")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        s.put("position_stop", "73920")
        account = asyncio.run(v.account())
        account["position"].position_amt = D(2)
        account["position"].liquidation_price = 73500  # only 0.5% gap, below the required 5%p
        account["position"].entry_price = 84000
        e.submit = lambda venue, key, req: _record(v, req)
        assert not asyncio.run(validate_actual_position(e, account, coin_market()))
        assert v.posts[-1]["reduce_only"] and v.posts[-1]["emergency"]
        assert s.get("halt") == "aggressive_actual_liquidation_or_exposure_violation"
    finally:
        s.close()


async def _record(v, req):
    v.posts.append(req)


def test_partial_spot_ioc_retries_only_three_times_same_day(tmp_path):
    cfg = Config(strategy_mode="aggressive", spot_fraction="1.0")
    v = FakeVenues()
    v.partial = True
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        account["permissions"]["permitsUniversalTransfer"] = True
        asyncio.run(e.initialize(account))
        spot = spot_market()
        market = coin_market()
        markets = {"spot": {"ETHBTC": spot}, "coinm": market}
        fees = {"spot": {"ETHBTC": D(".001")}, "coinm": D(".0005")}
        for _ in range(5):
            account = asyncio.run(v.account())
            asyncio.run(execute(e, account, markets, fees, D(".003")))
        assert len([post for post in v.posts if post[1] == "spot"]) <= 3
    finally:
        s.close()


def test_acknowledged_reverse_transfer_updates_owned_wallet_once(tmp_path, monkeypatch):
    cfg = Config(strategy_mode="aggressive", rebalance_mode="auto")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        account["permissions"]["permitsUniversalTransfer"] = True
        asyncio.run(e.initialize(account))
        account["permissions"]["permitsUniversalTransfer"] = True
        calls = []

        async def transfer(_gateway, amount, direction):
            calls.append((amount, direction))
            v.assets["BTC"] += amount
            return 123

        monkeypatch.setattr("btc_portfolio.transfer.signed_transfer", transfer)
        result = asyncio.run(rebalance_live(e, account, {"coinm": coin_market(), "spot": {"ETHBTC": spot_market()}},
            {"spot": {"ETHBTC": D(".001")}}, D("-.0003"), "2026-10"))
        assert result["status"] == "ACKNOWLEDGED" and result["type"] == "CMFUTURE_MAIN"
        assert calls == [(D(".00030000"), "CMFUTURE_MAIN")]
        assert D(s.get("wallet")["BTC"]) == D(".0015")
        e.check_spot(asyncio.run(v.account()))
    finally:
        s.close()


def test_uncertain_transfer_is_never_resubmitted(tmp_path, monkeypatch):
    cfg = Config(strategy_mode="aggressive", rebalance_mode="auto")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        account["permissions"]["permitsUniversalTransfer"] = True
        asyncio.run(e.initialize(account))
        account["permissions"]["permitsUniversalTransfer"] = True
        calls = []

        async def unknown(_gateway, amount, direction):
            calls.append(direction)
            raise TimeoutError()

        monkeypatch.setattr("btc_portfolio.transfer.signed_transfer", unknown)
        args = (e, account, {"coinm": coin_market(), "spot": {"ETHBTC": spot_market()}},
                {"spot": {"ETHBTC": D(".001")}}, D("-.0003"), "2026-10")
        assert asyncio.run(rebalance_live(*args))["status"] == "PENDING"
        assert asyncio.run(rebalance_live(*args))["status"] == "BLOCKED"
        assert len(calls) == 1 and s.get("aggressive_transfer")["phase"] == "PENDING"
        assert s.get("halt") == "transfer_outcome_requires_manual_reconciliation"
    finally:
        s.close()


def test_uncertain_transfer_history_is_read_only():
    calls = []

    class Transport:
        async def request(self, method, url, headers, timeout):
            calls.append((method, url))
            return SimpleNamespace(status=200, text=json.dumps({"rows": [
                {"tranId": 123, "type": "CMFUTURE_MAIN", "asset": "BTC", "amount": "0.0003", "status": "CONFIRMED"},
                {"tranId": 124, "type": "MAIN_CMFUTURE", "asset": "BTC", "amount": "0.0001"}]}))

    class Gateway:
        _api_key = "test-key"
        _api_secret = "test-secret"
        transport = Transport()

        async def _sync_time(self, *, force):
            return 260000

    rows = asyncio.run(transfer_history(Gateway(), "CMFUTURE_MAIN", 200000))
    assert [row["tran_id"] for row in rows] == [123]
    assert len(calls) == 1 and calls[0][0] == "GET" and "/sapi/v1/asset/transfer?" in calls[0][1]


def test_swing_ledger_migration_preview_preserves_registered_wallet(tmp_path, monkeypatch):
    from btc_portfolio import migration, portfolio_transfer
    root = tmp_path/"root"
    directory = tmp_path/"live"
    registry_path = root/"btc_portfolio/state/live-registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"uid_hash": hashlib.sha256(b"123").hexdigest(),
                                          "ledger": str((directory/"ledger.sqlite3").resolve())}))
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(portfolio_transfer, "ROOT", root)
    monkeypatch.setattr(migration, "service_stopped", lambda: True)
    monkeypatch.setattr(portfolio_transfer, "service_stopped", lambda: True)
    s = Store(directory/"ledger.sqlite3", "old-swing-binding", "live")
    s.put("uid", "123")
    s.put("wallet", {"BTC": ".0012", "ETH": "0", "BNB": "0", "SOL": "0", "XRP": "0"})
    s.put("reserve", {"BTC": ".001", "ETH": ".5", "BNB": "0", "SOL": "0", "XRP": "0"})
    s.close()
    before = snapshot(directory/"ledger.sqlite3")
    v = FakeVenues()
    account = asyncio.run(v.account())
    account["spot_bnb_burn"] = False
    account["permissions"]["permitsUniversalTransfer"] = True
    cfg = Config(strategy_mode="aggressive")
    plan = plan_snapshot(cfg, directory, before, account)
    assert plan["ready"] and plan["old_identity"] == "old-swing-binding"
    transfer = stopped_transfer_plan(cfg, account,
        {"coinm": coin_market(), "spot": {"ETHBTC": spot_market()}}, before, directory)
    assert transfer["ready"] and transfer["type"] == "CMFUTURE_MAIN"
    assert transfer["amount_btc"] == "0.00030000"
    assert snapshot(directory/"ledger.sqlite3") == before


def test_stopped_transfer_accepts_tiny_quote_drift_but_blocks_material_change():
    from btc_portfolio.portfolio_transfer import fresh_plan_is_safe
    initial = {"ready": True, "type": "CMFUTURE_MAIN", "amount_btc": "0.00029606"}
    one_satoshi = {"ready": True, "type": "CMFUTURE_MAIN", "amount_btc": "0.00029605"}
    changed = {"ready": True, "type": "CMFUTURE_MAIN", "amount_btc": "0.00028000"}
    reversed_plan = {"ready": True, "type": "MAIN_CMFUTURE", "amount_btc": "0.00029605"}
    assert fresh_plan_is_safe(initial, one_satoshi)
    assert not fresh_plan_is_safe(initial, changed)
    assert not fresh_plan_is_safe(initial, reversed_plan)


def test_reduce_only_flip_is_first_coin_action(tmp_path, monkeypatch):
    cfg = Config(strategy_mode="aggressive")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        asyncio.run(e.initialize(account))
        v.position = D(-1)
        account = asyncio.run(v.account())
        market = coin_market()
        monkeypatch.setattr("btc_portfolio.aggressive.validate_actual_position", _safe)
        result = asyncio.run(execute(e, account, {"spot": {"ETHBTC": spot_market()}, "coinm": market},
            {"spot": {"ETHBTC": D(".001")}, "coinm": D(".0005")}, D(".003")))
        assert result["action"] == "coinm_reduce_only_flip"
        assert v.posts[0][2]["reduce_only"] and v.posts[0][2]["side"] == "BUY"
        assert len(v.posts) == 1
    finally:
        s.close()


async def _safe(engine, account, market):
    return True


def test_kill_blocks_new_spot_and_coin_entries(tmp_path):
    cfg = Config(strategy_mode="aggressive")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        asyncio.run(e.initialize(account))
        s.put("aggressive_initial_equity", ".004")
        result = asyncio.run(execute(e, account, {"spot": {"ETHBTC": spot_market()}, "coinm": coin_market()},
            {"spot": {"ETHBTC": D(".001")}, "coinm": D(".0005")}, D(".0009")))
        assert result["aggressive_killed"] and not result["new_entries_allowed"]
        assert v.posts == [] and s.get("aggressive_killed")
    finally:
        s.close()


def test_third_asset_fee_halts_instead_of_corrupting_bnb_wallet(tmp_path):
    cfg = Config(strategy_mode="aggressive")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        asyncio.run(e.initialize(account))
        req = {"symbol": "ETHBTC", "side": "BUY", "quantity": ".01", "price": ".03"}
        ident, _ = s.intent("spot", "bnb-fee", req)
        asyncio.run(v.submit(ident, "spot", req))
        v.spots["ETHBTC"].rows[1][0]["commissionAsset"] = "BNB"
        asyncio.run(e.reconcile(s.pending()[0]))
        assert s.get("halt") == "external_fee_asset_requires_reconciliation"
        assert D(s.get("wallet")["ETH"]) == D(".01")
    finally:
        s.close()


def test_coin_resize_reconciliation_preserves_original_stop(tmp_path):
    cfg = Config(strategy_mode="aggressive")
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    try:
        e = Engine(v, s, cfg)
        account = asyncio.run(v.account())
        account["spot_bnb_burn"] = False
        asyncio.run(e.initialize(account))
        s.put("position_stop", "73920")
        s.put("position_entry", {"price": "84000", "stop_fraction": ".12", "entered_at_ms": 1})
        req = {"symbol": "BTCUSD_PERP", "side": "BUY", "quantity": "1", "price": "80000",
               "reduce_only": False, "stop": "73920", "aggressive_resize": True}
        ident, _ = s.intent("coinm", "resize", req)
        asyncio.run(v.submit(ident, "coinm", req))
        assert asyncio.run(e.reconcile(s.pending()[0]))
        assert s.get("position_stop") == "73920"
        assert s.get("position_entry")["price"] == "84000"
    finally:
        s.close()
