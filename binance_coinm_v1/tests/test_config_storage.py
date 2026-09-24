"""설정 검증 · 비밀값 가리기 · DB 기본 동작."""

import logging

import pytest

from binance_coinm_v1.config import ConfigError, Settings
from binance_coinm_v1.storage import Database, MASK, RedactingFormatter, Redactor

FAKE_KEY = "AbCdEf0123456789FakeKeyForTestsOnly"
FAKE_SECRET = "ZyXwVu9876543210FakeSecretForTests"


def test_defaults_are_safe():
    s = Settings.build()
    assert s.execution_mode == "paper"
    assert s.live_trading_enabled is False
    assert s.live_confirmation == ""
    assert s.symbol == "BTCUSD_PERP"
    assert s.margin_type == "ISOLATED" and s.position_mode == "ONE_WAY"
    assert s.leverage == 3
    assert s.max_positions == 1
    assert s.stop_trigger_type == "MARK_PRICE"       # MARKET_TRIGGER_TYPE 비움 -> 기본
    assert s.directions == (1, -1)
    assert s.risk_fraction == pytest.approx(0.005)


def test_from_mapping_env_names():
    s = Settings.from_mapping({
        "EXECUTION_MODE": "paper", "LEVERAGE": "3", "RISK_PER_TRADE_PCT": "0.5",
        "MARKET_TRIGGER_TYPE": "", "LADDER_TP_FRACTIONS": "0.3,0.3,0.2",
        "DIRECTIONS": "long", "LIVE_TRADING_ENABLED": "false"})
    assert s.stop_trigger_type == "MARK_PRICE"
    assert s.ladder_tp_fractions == (0.3, 0.3, 0.2)
    assert s.directions == (1,)
    s2 = Settings.from_mapping({"MARKET_TRIGGER_TYPE": "contract_price"})
    assert s2.stop_trigger_type == "CONTRACT_PRICE"


@pytest.mark.parametrize("kw", [
    {"margin_type": "CROSSED"}, {"position_mode": "HEDGE"}, {"leverage": 50},
    {"risk_per_trade_pct": 5}, {"max_positions": 2}, {"exit_mode": "zone"},
    {"stop_trigger_type": "LAST"}, {"ladder_tp_fractions": "0.5,0.5,0.5"},
    {"execution_mode": "testnet", "binance_env": "live"}, {"execution_mode": "yolo"},
    {"zone_interval": "1h"}, {"liq_guard_min_ratio": 0.5},
])
def test_invalid_settings_rejected(kw):
    with pytest.raises(ConfigError):
        Settings.build(**kw)


def test_secrets_not_in_repr_or_public_dict():
    s = Settings.build(api_key=FAKE_KEY, api_secret=FAKE_SECRET, telegram_token="123456:ABCDEFGHIJ",
                       live_confirmation="I_UNDERSTAND_LIVE_TRADING")
    text = repr(s) + str(s.public_dict())
    assert FAKE_KEY not in text and FAKE_SECRET not in text and "ABCDEFGHIJ" not in text
    assert "I_UNDERSTAND" not in text
    assert set(s.secrets()) == {FAKE_KEY, FAKE_SECRET, "123456:ABCDEFGHIJ"}


def test_load_reads_only_given_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EXECUTION_MODE=paper\nLEVERAGE=2\nUPBIT_OPEN_API_ACCESS_KEY=zzz\n", encoding="utf-8")
    s = Settings.load(env_file=env, environ={"LEVERAGE": "3"})
    assert s.leverage == 3          # 환경변수가 파일보다 우선
    assert s.execution_mode == "paper"


def test_fingerprint_changes_with_strategy_params():
    a = Settings.build()
    b = Settings.build(min_rr=1.5)
    c = Settings.build(telegram_token="999999:XYZXYZXYZ")          # 전략과 무관
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == c.fingerprint()
    assert a.fingerprint({"contractSize": 100}) != a.fingerprint({"contractSize": 10})


def test_redactor_masks_keys_signatures_and_objects():
    r = Redactor([FAKE_KEY, FAKE_SECRET])
    t = r(f"GET /dapi/v1/account?timestamp=1&signature=abcdef0123456789 key={FAKE_KEY} {FAKE_SECRET}")
    assert FAKE_KEY not in t and FAKE_SECRET not in t and "abcdef0123456789" not in t
    o = r.obj({"apiKey": "x", "nested": [{"signature": "s", "msg": f"k={FAKE_KEY}"}], "n": 1})
    assert o["apiKey"] == MASK and o["nested"][0]["signature"] == MASK
    assert FAKE_KEY not in o["nested"][0]["msg"] and o["n"] == 1
    assert "bot" + MASK in r("https://api.telegram.org/bot123456:ABCDEF/sendMessage")


def test_redacting_formatter_masks_log_output():
    r = Redactor([FAKE_SECRET])
    fmt = RedactingFormatter("%(message)s", redactor=r)
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "secret is %s", (FAKE_SECRET,), None)
    assert FAKE_SECRET not in fmt.format(rec)


def test_db_roundtrip_and_no_secrets(tmp_path):
    r = Redactor([FAKE_KEY, FAKE_SECRET])
    db = Database(str(tmp_path / "x.sqlite3"), redactor=r)
    db.upsert_order({"client_order_id": "cm1abc-EN0", "trade_id": "abc", "purpose": "ENTRY",
                     "symbol": "BTCUSD_PERP", "side": "BUY", "order_type": "MARKET",
                     "quantity": "3", "status": "PENDING_SUBMIT", "mode": "paper",
                     "raw": {"apiKey": FAKE_KEY, "note": f"s={FAKE_SECRET}"},
                     "last_error": f"boom {FAKE_KEY}"})
    db.upsert_order({"client_order_id": "cm1abc-EN0", "status": "FILLED", "executed_qty": "3",
                     "avg_price": "84000.1"})
    o = db.get_order("cm1abc-EN0")
    assert o["status"] == "FILLED" and o["purpose"] == "ENTRY" and o["quantity"] == "3"
    assert db.insert_fill({"symbol": "BTCUSD_PERP", "exchange_trade_id": 7, "price": 1.0,
                           "qty": 1, "mode": "paper"})
    assert not db.insert_fill({"symbol": "BTCUSD_PERP", "exchange_trade_id": 7, "price": 1.0,
                               "qty": 1, "mode": "paper"})        # 중복 체결 무시
    sid = db.insert_signal({"symbol": "BTCUSD_PERP", "pattern": "trendy_kangaroo",
                            "direction": 1, "bar_time": 1.0, "close_time": 3601.0}, "paper")
    assert sid is not None
    assert db.insert_signal({"symbol": "BTCUSD_PERP", "pattern": "trendy_kangaroo",
                             "direction": 1, "bar_time": 1.0, "close_time": 3601.0}, "paper") is None
    db.kv_set("paper:x", {"a": 1, "secret": FAKE_SECRET})
    assert db.kv_get("paper:x")["a"] == 1
    dump = db.dump_text()
    assert FAKE_KEY not in dump and FAKE_SECRET not in dump
    db.close()
