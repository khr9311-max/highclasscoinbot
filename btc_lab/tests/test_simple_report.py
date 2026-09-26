"""Beginner Telegram report: plain lines from the ledger, the bot status and Gemini."""
import json

from btc_lab import ledger as lg
from btc_lab import llm_judge as lj

T = 1_790_424_000_000                     # an hour boundary


def snap_at(t, close, equity=None, halt=None, waiting=False, qty=3, age=10):
    return {"bar_close_ms": t, "created_ms": t, "close": close, "mark": close, "daily_open": 84_000.0,
            "taker_bs": {"um": {"12": 1.05}}, "pred": {},
            "portfolio": None if equity is None else {"equity_btc": equity, "coin_qty": qty, "alt": "SOL",
                                                      "halt": halt, "waiting": waiting, "age_s": age}}


def test_status_file_is_read_without_touching_it(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"updated_at_ms": T - 5000, "coin_qty": "-2", "halt": None,
                                "wallet": {"BTC": "0.001", "SOL": "0"},
                                "result": {"equity_btc": "0.003", "alt_signal": {"symbol": "SOLBTC"},
                                           "coinm_timing_wait": {"timing": "waiting"}}}))
    p = lg.read_portfolio(path, T)
    assert p == {"equity_btc": 0.003, "coin_qty": -2.0, "alt": None, "halt": None, "waiting": True, "age_s": 5.0}
    assert lg.read_portfolio(tmp_path / "missing.json", T) is None


def test_report_is_short_and_plain(tmp_path):
    db = lg.open_db(tmp_path)
    for t, close, eq in ((T - 86_400_000, 83_000.0, 0.0030), (T - 3_600_000, 84_000.0, 0.0031)):
        lg.store(db, snap_at(t, close, eq))
    snap = snap_at(T, 84_840.0, 0.0033)
    text = lg.simple_report(db, snap, krw_rate=1400)
    lines = text.splitlines()
    assert len(lines) <= 7
    assert "0.003300 BTC" in text and "하루 +10.0%" in text and "1시간 +1.0%" in text
    assert "상승에 베팅 중(선물 롱) + 알트 SOL 보유" in text and "사려는 쪽이 조금 우세" in text
    assert "만원" in text
    db.close()


def test_report_warns_when_the_bot_is_halted_or_silent(tmp_path):
    db = lg.open_db(tmp_path)
    assert "⚠️ 봇 멈춤: protection_not_confirmed" in lg.simple_report(db, snap_at(T, 84_000.0, 0.003, halt="protection_not_confirmed"))
    assert "5분 넘게" in lg.simple_report(db, snap_at(T, 84_000.0, 0.003, age=900))
    assert "봇:" not in lg.simple_report(db, snap_at(T, 84_000.0))          # no status file configured
    db.close()


def test_gemini_lines_wait_for_enough_calls(tmp_path):
    db = lg.open_db(tmp_path)
    lg.store(db, snap_at(T, 84_000.0))
    lj.store(db, T, "m", {"direction_1h": "long", "direction_4h": "short", "confidence": 0.5, "reason": ""})
    lines = lj.simple_lines(db, T)
    assert lines[0] == "🧪 AI 연습 판단: 4시간 뒤 '내릴 것' (실제 매매엔 안 씀)"
    assert "채점 중" in lines[1]
    db.close()


def test_mood_words():
    assert lg.mood(1.2) == "사려는 쪽이 강함" and lg.mood(1.0) == "팽팽함" and lg.mood(0.8) == "팔려는 쪽이 강함"
    assert lg.mood(None) == "알 수 없음"
