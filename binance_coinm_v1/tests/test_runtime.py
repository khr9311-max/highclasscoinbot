"""13단계: 종이 매매 런타임 · 텔레그램 장애 격리 · 시작 차단 조건 (네트워크 없음)."""

import asyncio
import copy
import json

import pytest

from binance_coinm_v1.config import ConfigError, Settings
from binance_coinm_v1.exchange.errors import ContractResolutionError
from binance_coinm_v1.notifications import RecordingNotifier, TelegramNotifier
from binance_coinm_v1.runtime.accounting import build_snapshot
from binance_coinm_v1.runtime.bot import Bot
from binance_coinm_v1.storage import Database

from .conftest import run
from .fakes import FakeTransport
from .harness import Harness
from .helpers import FIXTURES, load_spec
from .test_execution import long_open

H = 3_600_000
NOW_MS = 1_790_240_000_000


def fake_exchange(xi=None):
    xi = xi or json.load(open(FIXTURES / "exchange_info_coinm.json", encoding="utf-8"))
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/time", lambda p: {"serverTime": NOW_MS})
    tr.add("GET", "/dapi/v1/exchangeInfo", lambda p: xi)
    tr.add("GET", "/dapi/v1/premiumIndex", lambda p: [{
        "symbol": "BTCUSD_PERP", "pair": "BTCUSD", "markPrice": "80000.0", "indexPrice": "80010.0",
        "estimatedSettlePrice": "80000", "lastFundingRate": "0.0001", "interestRate": "0.0001",
        "nextFundingTime": NOW_MS + 3 * H, "time": NOW_MS}])
    tr.add("GET", "/dapi/v1/ticker/price", lambda p: [{"symbol": "BTCUSD_PERP", "ps": "BTCUSD",
                                                       "price": "79995.0", "time": NOW_MS}])

    def klines(p):
        step = 4 * H if p["interval"] == "4h" else H
        n = int(p["limit"])
        end = (NOW_MS // step) * step
        return [[t, "80000", "80080", "79920", "80000", "10", t + step - 1, "0.1", 3, "5", "0.05", "0"]
                for t in range(end - (n - 1) * step, end + 1, step)]

    tr.add("GET", "/dapi/v1/klines", klines)
    return tr


class OneShotWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def recv(self):
        if self.msgs:
            return json.dumps(self.msgs.pop(0))
        await asyncio.sleep(3600)

    async def close(self):
        pass


def ws_connector(msgs):
    async def connect(url):
        return OneShotWS(msgs)
    return connect


def test_paper_bot_runs_without_any_signed_request(tmp_path):
    s = Settings.build(state_dir=str(tmp_path / "state"))
    tr = fake_exchange()
    notes = RecordingNotifier()
    msgs = [{"stream": "btcusd_perp@markPrice@1s", "data": {
                "e": "markPriceUpdate", "E": NOW_MS + 1000, "s": "BTCUSD_PERP", "p": "80001.0",
                "i": "80005.0", "r": "0.0001", "T": NOW_MS + 3 * H}},
            {"stream": "btcusd_perp@aggTrade", "data": {"e": "aggTrade", "E": NOW_MS + 1100,
                                                       "a": 1, "p": "80002.0", "q": "2",
                                                       "T": NOW_MS + 1100}}]
    bot = Bot(s, transport=tr, ws_connect=ws_connector(msgs), notifier=notes,
              clock=lambda: NOW_MS / 1000 + 5)
    run(bot.run(duration=0.5))
    assert all("X-MBX-APIKEY" not in c["headers"] for c in tr.calls)     # 서명 요청 0건
    assert all(c["method"] == "GET" for c in tr.calls)
    assert {"startup", "shutdown"} <= set(notes.kinds())
    assert bot.stats["market_events"] >= 2
    db = Database(s.db_path)
    try:
        assert db.latest_account_snapshot("paper")["equity_btc"] == pytest.approx(0.007)
        assert db.kv_get("paper:paper_state")["wallet"] == pytest.approx(0.007)
        assert db.events("paper", event="recovery")
    finally:
        db.close()


def test_bot_refuses_to_start_when_contract_not_tradable(tmp_path):
    xi = json.load(open(FIXTURES / "exchange_info_coinm.json", encoding="utf-8"))
    for sym in xi["symbols"]:
        if sym["symbol"] == "BTCUSD_PERP":
            sym["contractStatus"] = "PENDING_TRADING"
    s = Settings.build(state_dir=str(tmp_path / "state"))
    notes = RecordingNotifier()
    bot = Bot(s, transport=fake_exchange(xi), notifier=notes)
    with pytest.raises(ContractResolutionError):
        run(bot.setup())
    assert notes.messages and notes.messages[0][2] is True               # 치명 알림
    bot.db.close()


def test_live_mode_requires_keys(tmp_path):
    s = Settings.build(state_dir=str(tmp_path / "state"), execution_mode="live")
    bot = Bot(s, transport=fake_exchange(), notifier=RecordingNotifier())
    with pytest.raises(ConfigError):
        run(bot.setup())
    bot.db.close()


def test_snapshot_btc_usd_krw_separate():
    from binance_coinm_v1.exchange.models import AccountInfo, AssetBalance
    spec = load_spec()
    acct = AccountInfo({"BTC": AssetBalance("BTC", 0.007, 0.0001, 0.0071, 0.005,
                                            position_initial_margin=0.0012)}, [])
    snap = build_snapshot(acct, None, spec, mark=80000.0, index=80100.0, usd_krw=1400.0, source="t")
    assert snap["equity_btc"] == pytest.approx(0.0071)
    assert snap["equity_usd"] == pytest.approx(0.0071 * 80100.0)          # 지수가로 평가
    assert snap["equity_krw"] == pytest.approx(0.0071 * 80100.0 * 1400.0)
    assert snap["used_margin_btc"] == pytest.approx(0.0012)


# ---------------------------------------------------------------- 텔레그램 장애 격리
def test_telegram_failures_never_block_trading(tmp_path):
    async def go():
        calls = {"n": 0}

        async def broken_sender(msg):
            calls["n"] += 1
            if calls["n"] % 2:
                raise RuntimeError("telegram 502")
            await asyncio.sleep(30)                                       # 매달림 -> 타임아웃

        tg = TelegramNotifier("123456:FAKE-token-for-tests", "1", sender=broken_sender,
                              min_interval=0.0, timeout=0.05)
        await tg.start()
        h = Harness(tmp_path)
        h.engine.ctx.notifier = tg
        t = await long_open(h)                                            # 진입·보호 알림 다수
        await h.tick(79150.0)                                             # 손절
        assert h.trade is None and h.position() == 0
        assert h.db.get_position(t.trade_id)["close_reason"] == "stop"
        await asyncio.sleep(0.3)
        await tg.stop(flush_timeout=0.2)
        assert tg.failed >= 1                                             # 실패했지만 매매는 끝까지
    run(go())


def test_telegram_messages_are_redacted_and_bounded():
    sent = []

    async def sender(msg):
        sent.append(msg)

    async def go():
        tg = TelegramNotifier("123456:SECRET-TOKEN-XYZ", "1", sender=sender, min_interval=0.0,
                              max_queue=5)
        tg.redactor.add("apikey-AAAAAAAAAAAAAAAA")
        for k in range(20):
            tg.notify("signal", f"msg {k} apikey-AAAAAAAAAAAAAAAA")
        assert len(tg._q) == 5 and tg.dropped == 15
        await tg.start()
        await asyncio.sleep(0.1)
        await tg.stop()
    run(go())
    assert sent and all("AAAAAAAAAAAAAAAA" not in m and "SECRET-TOKEN" not in m for m in sent)
    assert all("KST" in m for m in sent)                                  # 표시는 KST
