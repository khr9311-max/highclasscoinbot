import asyncio
import json
import sqlite3
import time

from btc_portfolio import notify
from btc_spot.notify import DeliveryLedger


def test_recovery_notice_is_sent_once_after_error(tmp_path, monkeypatch):
    (tmp_path / "status.json").write_text(json.dumps({
        "updated_at_ms": time.time_ns() // 1_000_000,
        "result": {"status": "READY"}, "coin_qty": "0", "pending": 0,
        "wallet": {"BTC": "0.001"}, "halt": None,
    }), encoding="utf-8")
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")

    delivery = DeliveryLedger(tmp_path)
    delivery.mark("portfolio:status:2026-09-25:READY:None", 1)
    delivery.mark("portfolio:status:2026-09-25:ERROR:None", 2)
    messages = []

    async def send(_session, _config, body):
        messages.append(body)
        return 3

    monkeypatch.setattr(notify, "send_message", send)
    try:
        asyncio.run(notify.dispatch(tmp_path, None, delivery, None, None))
        asyncio.run(notify.dispatch(tmp_path, None, delivery, None, None))
    finally:
        delivery.close()
    assert len(messages) == 1
    assert "이전 오류에서 복구됨" in messages[0]
