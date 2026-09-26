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
    labels = {"aggressive_alt_rotation": "알트 교체", "aggressive_coin_flip": "COIN-M 방향 전환",
              "aggressive_coin_entry": "COIN-M 진입", "aggressive_coin_resize": "COIN-M 계약 조정",
              "aggressive_rebalance_alert": "월간 재배분 필요", "aggressive_rebalance": "월간 재배분",
              "aggressive_transfer_uncertain": "BTC 이체 결과 미확정",
              "aggressive_transfer_acknowledged": "BTC 내부 이체 확인",
              "aggressive_margin_violation": "청산가 안전거리 위반",
              "aggressive_kill": "운용자산 비상 정지", "stop_filled": "COIN-M 재난손절 체결"}
    if kind in labels:
        return f"[BTC 포트폴리오 #{event_id}] {labels[kind]}\n"+json.dumps(payload, ensure_ascii=False, default=str)[:2500]
    return f"[BTC 포트폴리오 #{event_id}] {kind}\n"+json.dumps(payload, ensure_ascii=False)[:2500]


async def dispatch(directory, config, delivery, fx, session):
    state = json.loads((directory/"status.json").read_text(encoding="utf-8"))
    with sqlite3.connect(f"file:{(directory/'ledger.sqlite3').as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,kind,payload FROM events WHERE kind IN ('fill','stop_filled','protection_failure',"
                          "'aggressive_alt_rotation','aggressive_coin_flip','aggressive_coin_entry',"
                          "'aggressive_coin_resize','aggressive_rebalance_alert','aggressive_rebalance',"
                          "'aggressive_transfer_uncertain','aggressive_transfer_acknowledged',"
                          "'aggressive_margin_violation','aggressive_kill') ORDER BY id").fetchall()
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
    if status == "ERROR" and not result.get("reason"):
        result["reason"] = str(result.get("error_type") or "unknown_error") + (" @ " + str(result["error_site"]) if result.get("error_site") else "")
    key = "portfolio:status:"+datetime.now(KST).date().isoformat()+":"+str(status)+":"+str(state.get("halt"))+(":"+str(result.get("reason")) if status == "ERROR" else "")
    if status == "ERROR":
        key += ":"+datetime.now(KST).strftime("%H")
    recovered = False
    if status == "READY":
        last_error = delivery.db.execute("SELECT rowid,event_id FROM sent WHERE event_id LIKE ? ORDER BY rowid DESC LIMIT 1",
                                         ("portfolio:status:%:ERROR:%",)).fetchone()
        last_ready = delivery.db.execute("SELECT rowid FROM sent WHERE event_id LIKE ? ORDER BY rowid DESC LIMIT 1",
                                         ("portfolio:status:%:READY:%",)).fetchone()
        last_recovery = delivery.db.execute("SELECT rowid FROM sent WHERE event_id LIKE ? ORDER BY rowid DESC LIMIT 1",
                                            ("portfolio:recovered:%",)).fetchone()
        if last_error and last_error[0] > max(last_ready[0] if last_ready else 0,
                                               last_recovery[0] if last_recovery else 0):
            key = "portfolio:recovered:"+last_error[1]
            recovered = True
    if not delivery.sent(key):
        manual = state.get("coinm_managed") is False
        coin = "COIN-M 직접 관리(봇 미사용)" if manual else f"COIN-M 계약 {state.get('coin_qty')}"
        body = (f"[BTC 포트폴리오 상태] {status}\n최근 갱신 {age}초 전\n"
                f"{coin} · 미확정 주문 {state.get('pending')}\n"
                f"현물 원장 {state.get('wallet')}\n차단 사유 {state.get('halt') or result.get('reason') or result.get('reasons') or '-'}")
        if recovered:
            body += "\n이전 오류에서 복구됨"
        if state.get("strategy_mode"):
            body += f"\n전략 {state['strategy_mode']} · 평가 목표 간격 {state.get('evaluation_interval_seconds')}초"
        if result.get("equity_btc"):
            body += f"\nBTC 환산 운용자산 {result['equity_btc']} BTC"
        allocation = result.get("allocation")
        if allocation and not manual:
            from decimal import Decimal
            spot_pct = Decimal(allocation["spot_weight_pct"])
            coin_pct = Decimal(allocation["coinm_weight_pct"])
            body += (f"\n운용 배분 현물 {spot_pct:.1f}% / COIN-M {coin_pct:.1f}%"
                     f" (목표 {Decimal(allocation['target_spot_weight_pct']):.0f}% /"
                     f" {Decimal(allocation['target_coinm_weight_pct']):.0f}%)")
            if allocation["rebalance_review"]:
                if state.get("strategy_mode") == "aggressive":
                    body += (f"\n월간 재배분 대상: 현물 목표 초과분 {allocation['spot_excess_btc']} BTC"
                             " (실행 여부는 설정과 월간 평가 시점에 따름)")
                else:
                    body += (f"\n재배분 검토: 현물 목표 초과분 {allocation['spot_excess_btc']} BTC"
                             " (자동 이체·위험한도 변경 없음)")
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
