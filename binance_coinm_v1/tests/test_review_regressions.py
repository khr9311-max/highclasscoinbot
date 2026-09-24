"""Review regressions: failed cleanup, offline fills and recovery ownership."""

from decimal import Decimal

import pytest

from binance_coinm_v1.exchange.errors import NetworkError
from binance_coinm_v1.exchange.models import OrderRequest
from binance_coinm_v1.exchange.paper_gateway import Fault
from binance_coinm_v1.execution import order_ids as ids

from .conftest import run
from .harness import Harness
from .test_execution import long_open
from .test_recovery import restart


def test_failed_stop_cleanup_keeps_trade_closing_until_retry(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        stop = t.orders["stop"]
        real_cancel = h.paper.cancel_order

        async def fail_stop(symbol, cid, is_algo):
            if cid == stop:
                raise NetworkError("cancel unavailable")
            return await real_cancel(symbol, cid, is_algo)

        h.paper.cancel_order = fail_stop
        assert not await h.engine.exits.close(t, "time")
        assert h.position() == 0
        assert h.db.get_position(t.trade_id)["state"] == "CLOSING"
        assert h.paper.algos[stop]["status"] == "NEW"
        h.paper.cancel_order = real_cancel
        await h.engine.reconcile("retry")
        assert h.db.get_position(t.trade_id)["state"] == "CLOSED"
        assert not h.algos()
    run(go())


@pytest.mark.parametrize("partial", [False, True])
def test_offline_tp_is_reconciled_before_replacement(tmp_path, partial):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        original = t.orders["tp0"]
        planned = h.paper.algos[original]["qty"]
        h.paper.set_event_sink(lambda ev: None)
        if partial:
            apply = h.paper._apply_trade

            def partial_fill(order, side, qty, price, **kw):
                apply(order, side, Decimal(1), price, **kw)
                order["status"] = "EXPIRED"
                h.paper._apply_trade = apply

            h.paper._apply_trade = partial_fill
        h.paper.update_market(last=81100, mark=81100)
        filled = h.paper.algos[original]["actual_qty"]
        assert filled > 0
        h2 = restart(h, tmp_path)
        await h2.start(81100)
        await h2.engine.reconcile("periodic")
        await h2.engine.reconcile("periodic")
        assert sum((o["executed"] for o in h2.paper.orders.values()
                    if o["side"] == "SELL"), Decimal(0)) == planned
        assert 0 in h2.trade.tp_filled
        assert h2.position() == Decimal(15) - planned
    run(go())


def test_adopted_orphan_keeps_valid_stop_after_cleanup_and_restart(tmp_path):
    async def go():
        h = Harness(tmp_path)
        h.paper.update_market(last=80000, mark=80000)
        await h.paper.place_order(OrderRequest("manual", "BTCUSD_PERP", "BUY", "MARKET",
                                               quantity=Decimal(5)))
        old = "cm1deadbeef00-SL0"
        await h.paper.place_order(OrderRequest(old, "BTCUSD_PERP", "SELL", "STOP_MARKET",
                                               trigger_price=Decimal(79000), close_position=True,
                                               working_type="MARK_PRICE"))
        await h.start(80000)
        stops = [a for a in h.algos() if a["type"] == "STOP_MARKET"]
        assert stops and h.trade.orders["stop"] in [a["client_id"] for a in stops]
        assert all(a["side"] == "SELL" and a["close_position"] for a in stops)
        h2 = restart(h, tmp_path)
        await h2.start(80000)
        assert "orphan" in h2.engine.halts
        assert not h2.engine._entry_gate()[0]
    run(go())


def test_failed_orphan_cancel_blocks_new_entries_until_reconciled(tmp_path):
    async def go():
        h = Harness(tmp_path)
        h.paper.update_market(last=80000, mark=80000)
        await h.paper.place_order(OrderRequest("cm1deadbeef00-SL0", "BTCUSD_PERP", "SELL",
                                               "STOP_MARKET", trigger_price=Decimal(79000),
                                               close_position=True))
        h.paper.inject(Fault("cancel", NetworkError("down")))
        rep = await h.start(80000)
        assert not rep["trading_allowed"]
        assert not h.engine._entry_gate()[0]
        assert (await h.engine.reconcile("retry"))["trading_allowed"]
    run(go())


def test_mark_updates_cannot_hide_stale_contract_price(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000)
        h.clock.t += 60
        await h.engine.on_market(mark=80010, ts_ms=int(h.clock.t * 1000))
        assert not h.engine._entry_gate()[0]
        await h.engine.on_market(last=80005, ts_ms=int(h.clock.t * 1000))
        assert h.engine._entry_gate()[0]
        await h.engine.on_market(last=90000, ts_ms=int((h.clock.t - 60) * 1000))
        assert h.engine.ctx.market.last == 80005
    run(go())


@pytest.mark.parametrize("over", [
    {"slippage_bps": "nan"}, {"ladder_tp_fractions": "nan,0,0"},
    {"max_exposure_multiple": float("inf")}, {"validation_max_age_days": -1},
    {"validation_min_dsr": 1.01}, {"market_ws_stale_sec": 0},
])
def test_invalid_numeric_settings_fail_closed(over):
    from binance_coinm_v1.config import ConfigError, Settings
    with pytest.raises(ConfigError):
        Settings.build(**over)


@pytest.mark.parametrize("over", [
    {"max_exposure_multiple": 2}, {"liq_guard_min_ratio": 3},
    {"max_daily_loss_pct": 1}, {"stop_price_protect": True},
])
def test_old_report_cannot_approve_changed_risk_settings(db, over):
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.validation import ValidationGate, build_report
    from .test_validation import ESS, NOW, good_backtest, paper
    s = Settings.build()
    db.insert_validation_report(build_report(s, good_backtest(s), paper(), ESS, now=NOW))
    assert not ValidationGate(Settings.build(**over), db, ESS, clock=lambda: NOW).check()[0]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1_790_000_001.0])
def test_invalid_report_timestamp_cannot_open_gate(db, value):
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.validation import ValidationGate, build_report
    from .test_validation import ESS, NOW, good_backtest, paper
    s = Settings.build()
    report = build_report(s, good_backtest(s), paper(), ESS, now=NOW)
    report["generated_at"] = value
    import json
    db.conn.execute("INSERT INTO validation_reports (ts, passed, report) VALUES (?, ?, ?)",
                    (NOW, 1, json.dumps(report)))
    assert not ValidationGate(s, db, ESS, clock=lambda: NOW).check()[0]


def test_stricter_validation_policy_requires_new_report(db):
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.validation import ValidationGate, build_report
    from .test_validation import ESS, NOW, good_backtest, paper
    s = Settings.build()
    db.insert_validation_report(build_report(s, good_backtest(s), paper(), ESS, now=NOW))
    strict = Settings.build(validation_min_trades=1000)
    assert not ValidationGate(strict, db, ESS, clock=lambda: NOW).check()[0]


@pytest.mark.parametrize("over", [
    {"chosen_summary.avg_return": float("inf")}, {"trial.dsr": float("inf")},
    {"trial.pbo": -1}, {"chosen_summary.max_drawdown": -1},
])
def test_invalid_backtest_metrics_cannot_pass(over):
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.validation import build_report
    from .test_validation import ESS, NOW, good_backtest, paper
    s = Settings.build()
    assert not build_report(s, good_backtest(s, **over), paper(), ESS, now=NOW)["passed"]


def test_paper_samples_require_same_configuration_and_live_market(db):
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.validation import paper_trade_returns
    from .test_validation import ESS, NOW
    fp = Settings.build().fingerprint(ESS)
    base = dict(symbol="BTCUSD_PERP", mode="paper", direction=1, state="CLOSED",
                entry_avg_price=80000, equity_at_entry_btc=0.01, closed_at=NOW,
                signal={"close_time": NOW - 1}, accounting={"net_pnl_btc": 0.0001, "accounting_complete": True},
                validation_fingerprint=fp, market_environment="live")
    for tid, fields in (("valid", {}), ("legacy", {"validation_fingerprint": None}),
                        ("changed", {"validation_fingerprint": "different"}),
                        ("testnet", {"market_environment": "testnet"})):
        db.upsert_position(dict(base, trade_id=tid) | fields)
    assert [r["trade_id"] for r in paper_trade_returns(db, NOW - 100, "BTCUSD_PERP", fp)] == ["valid"]


@pytest.mark.parametrize("failure", ["snapshot", "worker"])
def test_runtime_failure_stops_workers_and_closes_resources(tmp_path, failure):
    import asyncio
    from binance_coinm_v1.config import Settings
    from binance_coinm_v1.notifications import RecordingNotifier
    from binance_coinm_v1.runtime.bot import Bot
    from .test_runtime import fake_exchange, ws_connector, NOW_MS

    async def go():
        bot = Bot(Settings.build(state_dir=str(tmp_path)), transport=fake_exchange(),
                  ws_connect=ws_connector([]), notifier=RecordingNotifier(),
                  clock=lambda: NOW_MS / 1000 + 5)

        async def broken():
            raise RuntimeError("injected runtime failure")

        if failure == "snapshot":
            bot.snapshot = broken
        else:
            bot._event_pump = broken
        with pytest.raises(RuntimeError, match="injected runtime failure"):
            await asyncio.wait_for(bot.run(duration=10), timeout=2)
        assert bot.stop_event.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert "shutdown" in bot.notifier.kinds()
    run(go())


@pytest.mark.parametrize("over", [
    {"signal_interval": "15m"}, {"zone_interval": "8h"},
    {"entry_trigger_type": "MARK_PRICE"}, {"stop_price_protect": True},
])
def test_backtest_rejects_settings_it_cannot_simulate(over):
    from binance_coinm_v1.config import ConfigError, Settings
    from binance_coinm_v1.backtest.runner import run_backtest
    with pytest.raises(ConfigError):
        run_backtest(Settings.build(**over), save=False)


def test_backtest_daily_loss_limit_skips_later_signal():
    from binance_coinm_v1.backtest.simulator import SimConfig
    from .test_backtest import rows_flat, run_sim, sig
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79500, 79900)
    rows[73] = (80000, 80300, 79500, 79900)
    signals = {(i, 1): sig(i, 1, 80200, 79600, []) for i in (70, 72)}
    limited = run_sim(rows, signals, SimConfig(slippage_bps=0, stop_slippage_bps=0,
                                               max_daily_loss_pct=0.1))
    unrestricted = run_sim(rows, signals, SimConfig(slippage_bps=0, stop_slippage_bps=0))
    assert len(unrestricted.trades) == 2 and len(limited.trades) == 1
    assert limited.skipped[0]["skip_reason"] == "daily_loss_limit"


def test_order_snapshot_cannot_lose_fill_quantity_on_late_message(db):
    db.upsert_order(dict(client_order_id="cm1deadbeef00-TP0_0", mode="paper", status="FINISHED",
                         executed_qty=3, avg_price=81000, is_algo=True, purpose="TP1",
                         symbol="BTCUSD_PERP", side="SELL", order_type="TAKE_PROFIT_MARKET"))
    db.upsert_order(dict(client_order_id="cm1deadbeef00-TP0_0", status="NEW",
                         executed_qty=0, avg_price=0))
    row = db.get_order("cm1deadbeef00-TP0_0")
    assert row["status"] == "FINISHED" and Decimal(row["executed_qty"]) == 3
    assert float(row["avg_price"]) == 81000


def test_terminal_stop_cannot_be_treated_as_active_protection(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000)
        original = h.paper.get_algo_order

        async def finished_stop(symbol, cid):
            st = await original(symbol, cid)
            if st.order_type == "STOP_MARKET":
                h.paper.algos[cid]["status"] = "FINISHED"
                st.status = "FINISHED"
            return st

        h.paper.get_algo_order = finished_stop
        t = h.arm(1, 80100, 79200, [])
        await h.tick(80150)
        assert h.position() == 0
        assert h.db.get_position(t.trade_id)["close_reason"] == "protect_failed"
    run(go())


@pytest.mark.parametrize("change", [{"margin_type": "cross"}, {"leverage": 20}])
def test_entry_rechecks_actual_account_configuration(tmp_path, change):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000)
        h.engine.ctx.mode = "live"
        for name, value in change.items():
            setattr(h.paper, name, value)
        t = h.arm(1, 80100, 79200, [])
        await h.tick(80150)
        assert h.position() == 0
        assert h.db.get_position(t.trade_id)["close_reason"] == "account_configuration_mismatch"
    run(go())
