"""Read-only event outbox. Notification failure never blocks trading or stop protection."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from binance_coinm_v1.runtime.accounting import FxProvider
from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_spot.notify import DeliveryLedger, KST, send_message, settings
from btc_spot.config import DEFAULT_CREDENTIALS
from btc_spot.runtime import safe_error
from .config import ROOT


def format_event(event_id, kind, payload):
    if kind == "fill":
        venue, symbol = payload["venue"], payload["symbol"]
        side = payload.get("side") or ("BUY" if payload.get("isBuyer") else "SELL")
        qty = payload.get("qty")
        fee = payload.get("commission")
        asset = payload.get("commissionAsset", payload.get("commission_asset"))
        body = f"[BTC 포트폴리오 체결 #{event_id}]\n{venue} {symbol} {side}\n수량 {qty} · 가격 {payload.get('price')}\n수수료 {fee} {asset}"
        if venue == "coinm":
            body += f"\n실현손익 {payload.get('realized_pnl_btc')} BTC"
        else:
            body += f"\n체결금액 {payload.get('quoteQty')} BTC"
        return body
    return f"[BTC 포트폴리오 #{event_id}] {kind}\n"+json.dumps(payload, ensure_ascii=False)[:2500]


async def dispatch(directory, config, delivery, fx, session):
    state = json.loads((directory/"status.json").read_text(encoding="utf-8"))
    with sqlite3.connect(f"file:{(directory/'ledger.sqlite3').as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,kind,payload FROM events WHERE kind IN ('fill','stop_filled','protection_failure') ORDER BY id").fetchall()
    for ident, kind, raw in rows:
        key = f"portfolio:event:{ident}"
        if not delivery.sent(key):
            payload = json.loads(raw)
            body = format_event(ident, kind, payload)
            if kind == "fill" and state.get("result", {}).get("btc_usd_reference"):
                btc = payload.get("quoteQty") if payload["venue"] == "spot" else payload.get("realized_pnl_btc")
                if btc is not None:
                    rate = await fx.usd_krw()
                    won = float(btc)*float(state["result"]["btc_usd_reference"])*rate
                    body += f"\n참고 원화 {'체결금액' if payload['venue']=='spot' else '실현손익'} 약 {won:,.0f}원 ({fx.last_source})"
            delivery.mark(key, await send_message(session, config, body))
    age = max(0, int(time.time()-state["updated_at_ms"]/1000))
    result = state.get("result", {})
    status = "STALE" if age > 180 else result.get("status")
    key = "portfolio:status:"+datetime.now(KST).date().isoformat()+":"+str(status)+":"+str(state.get("halt"))
    if not delivery.sent(key):
        body = (f"[BTC 포트폴리오 상태] {status}\n최근 갱신 {age}초 전\n"
                f"COIN-M 계약 {state.get('coin_qty')} · 미확정 주문 {state.get('pending')}\n"
                f"현물 원장 {state.get('wallet')}\n차단 사유 {state.get('halt') or result.get('reason') or result.get('reasons') or '-'}")
        if result.get("equity_btc"):
            body += f"\nBTC 환산 운용자산 {result['equity_btc']} BTC"
        delivery.mark(key, await send_message(session, config, body))


async def main_loop(args):
    import aiohttp
    config = settings(args.credentials_file)
    lock = InstanceLock(args.state_dir/"notify.lock")
    lock.acquire()
    delivery = DeliveryLedger(args.state_dir)
    fx = FxProvider("upbit_usdt", config.fallback_krw)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            while True:
                try:
                    await dispatch(args.state_dir, config, delivery, fx, session)
                except Exception as exc:
                    print(json.dumps(safe_error(exc)), flush=True)
                if args.once:
                    return
                await asyncio.sleep(30)
    finally:
        delivery.close()
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--state-dir", type=Path, default=ROOT/"btc_portfolio/state/live")
    parser.add_argument("--once", action="store_true")
    asyncio.run(main_loop(parser.parse_args()))
