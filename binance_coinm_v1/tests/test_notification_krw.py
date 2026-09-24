"""Human-facing BTC and KRW amounts must use the same contract and FX basis."""

import pytest
import time

from binance_coinm_v1.config import Settings
from binance_coinm_v1.notifications import RecordingNotifier
from binance_coinm_v1.runtime.bot import Bot
from binance_coinm_v1.runtime.accounting import FxProvider

from .conftest import run
from .harness import Harness
from .test_execution import long_open
from .test_runtime import fake_exchange


def test_entry_tp_stop_and_close_messages_show_krw_basis(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        entry = next(msg for kind, msg, _ in h.notes.messages if kind == "entry")
        assert "0.01871491 BTC" in entry
        assert "약 2,085,000원" in entry
        assert "USD/KRW 1,390원 (설정 환율)" in entry

        await h.tick(81100)
        tp = next(msg for kind, msg, _ in h.notes.messages if kind == "tp")
        assert "약 417,000원" in tp
        assert "BTC" in tp

        await h.tick(79100)
        stop = next(msg for kind, msg, _ in h.notes.messages if kind == "stop")
        close = next(msg for kind, msg, _ in h.notes.messages if kind == "close")
        assert "계약 명목가치" in stop and "원" in stop
        acc = h.db.get_position(t.trade_id)["accounting"]
        assert f"{acc['net_pnl_krw']:+,.0f} KRW" in close
        assert "USD/KRW 1,390원 (설정 환율)" in close
    run(go())


def test_funding_message_uses_event_mark_and_shows_krw(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        ft = int((h.clock.t + 1) * 1000)
        h.paper.income.append(dict(symbol=h.spec.symbol, incomeType="FUNDING_FEE",
                                   income="-0.00001", asset="BTC", time=ft,
                                   tranId="event-9", markPrice=80500))
        h.clock.t += 1
        assert await h.engine.ctx.sync_funding(t, ft) == 1
        msg = next(msg for kind, msg, _ in h.notes.messages if kind == "funding")
        assert "-0.00001000 BTC" in msg and "-1,119원" in msg
        assert "USD/KRW 1,390원" in msg
        assert await h.engine.ctx.sync_funding(t, ft) == 0
        assert len([kind for kind, _, _ in h.notes.messages if kind == "funding"]) == 1
    run(go())


def test_startup_shows_account_equity_in_btc_and_krw(tmp_path):
    async def go():
        settings = Settings.build(state_dir=str(tmp_path / "state"))
        notes = RecordingNotifier()
        bot = Bot(settings, transport=fake_exchange(), notifier=notes,
                  clock=lambda: 1_790_240_005.0)
        try:
            await bot.setup()
            msg = next(msg for kind, msg, _ in notes.messages if kind == "startup")
            assert "계좌 평가 0.00700000 BTC" in msg
            assert "약 778,497원" in msg
            assert "BTC 기준 80,010.0 USD × USD/KRW 1,390원 (설정 환율)" in msg
        finally:
            await bot.shutdown()
    run(go())


def test_missing_or_invalid_fill_price_never_invents_krw(tmp_path):
    h = Harness(tmp_path)
    assert h.engine.ctx.contract_value(1, None) == "계약 명목가치 원화 환산 미확정"
    assert h.engine.ctx.contract_value(1, float("nan")) == "계약 명목가치 원화 환산 미확정"


def test_public_fx_refreshes_and_rejects_stale_or_invalid_quotes(monkeypatch):
    async def go():
        monkeypatch.delenv("COINM_V1_TEST_MODE", raising=False)
        p = FxProvider("upbit_usdt", 1390, ttl=30)
        quotes = [(1376.0, time.time()), (1381.0, time.time())]
        calls = []
        async def fetch():
            calls.append(1)
            return quotes.pop(0)
        p._fetch_quote = fetch
        assert await p.usd_krw() == 1376.0 and p.last_source == "upbit_usdt"
        assert await p.usd_krw() == 1376.0 and len(calls) == 1
        p._at -= 31
        p._last_attempt -= 31
        assert await p.usd_krw() == 1381.0 and len(calls) == 2
        async def stale():
            return 999.0, time.time() - 3600
        p._fetch_quote = stale
        p._quote_at -= 180
        p._at -= 31
        p._last_attempt -= 31
        assert await p.usd_krw() == 1390.0 and p.last_source == "fixed_fallback"
    run(go())


def test_bot_uses_refreshed_quote_in_message_without_account_requests(tmp_path, monkeypatch):
    async def go():
        monkeypatch.delenv("COINM_V1_TEST_MODE", raising=False)
        settings = Settings.build(state_dir=str(tmp_path / "state"),
                                  usd_krw_source="upbit_usdt")
        notes = RecordingNotifier()
        bot = Bot(settings, transport=fake_exchange(), notifier=notes,
                  clock=lambda: 1_790_240_005.0)
        quotes = [1376.0, 1381.0]
        async def fetch():
            return quotes.pop(0), time.time()
        bot.fx._fetch_quote = fetch
        try:
            await bot.setup()
            assert bot.engine.ctx.usd_krw == 1376.0
            msg = next(msg for kind, msg, _ in notes.messages if kind == "startup")
            assert "USDT/KRW 1,376원 (공개 참고 시세, 1 USDT≈1 USD 가정)" in msg
            bot.fx._at -= 31
            bot.fx._last_attempt -= 31
            await bot._refresh_fx()
            assert bot.engine.ctx.usd_krw == 1381.0
            assert "USDT/KRW 1,381원" in bot.engine.ctx.contract_value(1, 80000)
        finally:
            await bot.shutdown()
    run(go())
