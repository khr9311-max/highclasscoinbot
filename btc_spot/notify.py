"""Read-only Telegram companion for the BTC Spot runtime.

The trading process never waits for Telegram or the public FX endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from binance_coinm_v1.runtime.accounting import FxProvider
from binance_coinm_v1.runtime.instance_lock import InstanceLock
from .config import DEFAULT_CREDENTIALS, ROOT, state_directory
from .runtime import safe_error

KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)
    fallback_krw: float = 1390.0


def settings(path=DEFAULT_CREDENTIALS):
    from dotenv import dotenv_values
    path = Path(path).resolve()
    if path == (ROOT / '.env').resolve() or not path.is_file():
        raise ValueError('Use an existing dedicated Binance .env file')
    values = dotenv_values(path, interpolate=False)
    token, chat = values.get('TELEGRAM_BOT_TOKEN'), values.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise ValueError('Telegram bot token and chat ID are required')
    try:
        fallback = float(values.get('USD_KRW_RATE') or '1390')
    except ValueError:
        raise ValueError('Invalid fallback KRW rate') from None
    if not 500 < fallback < 5000:
        raise ValueError('Invalid fallback KRW rate')
    return Settings(token, chat, fallback)


def snapshot(directory):
    path = Path(directory) / 'status.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('mode') != 'live' or state.get('orders_enabled') is not True:
        raise ValueError('Notification state must be live Spot')
    if not isinstance(state.get('updated_at_ms'), int):
        raise ValueError('Invalid heartbeat')
    return state


def fills(directory):
    path = Path(directory) / 'ledger.sqlite3'
    if not path.is_file():
        return []
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        binding = db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
        if not binding or json.loads(binding[0]).get('mode') != 'live':
            raise ValueError('Notification ledger must be live Spot')
        return [dict(row) for row in db.execute(
            'SELECT symbol,trade_id,decision_id,payload,btc_delta,quote_delta '
            'FROM fills ORDER BY rowid')]


def _amount(value, places=8):
    return f'{Decimal(str(value)):,.{places}f}'


def _krw(value):
    return f'{Decimal(str(value)):,.0f}'


def _basis(rate_source):
    return ('업비트 USDT/KRW 참고 시세' if rate_source == 'upbit_usdt' else
            '최근 업비트 USDT/KRW 참고 시세' if rate_source == 'upbit_usdt_cached' else
            '설정 환율 대체값')


def status_text(state, rate, rate_source):
    wallet = state['ledger']['wallet']
    bid = Decimal(state['market']['bid'])
    fx = Decimal(str(rate))
    held_btc = Decimal(wallet['btc'])
    held_usdt = Decimal(wallet['quote'])
    reserve_btc = Decimal(wallet['reserve_btc'])
    reserve_usdt = Decimal(wallet['reserve_quote'])
    strategy_btc_est = held_btc + held_usdt / bid
    total_btc_est = strategy_btc_est + reserve_btc + reserve_usdt / bid
    strategy_krw = (held_btc * bid + held_usdt) * fx
    total_krw = ((held_btc + reserve_btc) * bid + held_usdt + reserve_usdt) * fx
    at = datetime.fromtimestamp(state['updated_at_ms'] / 1000, KST).strftime('%Y-%m-%d %H:%M KST')
    decision = state.get('decision') or {}
    target = Decimal(str(decision.get('target_btc_fraction', 0))) * 100
    return (f'📊 BTC 현물 운용 현황 ({at})\n'
            f'상태: {state.get("status", "UNKNOWN")} | 목표 BTC 비중: {target:g}%\n'
            f'운용: BTC { _amount(held_btc) } + USDT { _amount(held_usdt, 2) }\n'
            f'운용 평가: BTC 약 {_amount(strategy_btc_est)} / 약 {_krw(strategy_krw)}원\n'
            f'예비: BTC {_amount(reserve_btc)} + USDT {_amount(reserve_usdt, 2)}\n'
            f'전체 평가: BTC 약 {_amount(total_btc_est)} / 약 {_krw(total_krw)}원\n'
            f'체결 {state["ledger"]["fill_count"]}건 | 미확정 주문 {len(state["ledger"]["pending"])}건\n'
            f'BTC/USDT 매도호가 {_amount(bid, 2)} | USDT/KRW {_amount(rate, 2)}\n'
            f'원화는 {_basis(rate_source)}를 쓴 추정치이며 수수료·스프레드 전입니다.')


def fill_text(row, rate, rate_source):
    trade = json.loads(row['payload'])
    side = '매수' if trade['side'] == 'BUY' else '매도'
    quote = Decimal(trade['quote_quantity'])
    return (f'✅ BTC 현물 {side} 체결\n'
            f'결정일(UTC): {row["decision_id"]} | 체결 ID: {row["trade_id"]}\n'
            f'수량: {_amount(trade["quantity"])} BTC\n'
            f'체결가: {_amount(trade["price"], 2)} USDT/BTC\n'
            f'거래금액: {_amount(quote, 2)} USDT / 약 {_krw(quote * Decimal(str(rate)))}원\n'
            f'수수료: {_amount(trade["fee"])} {trade["fee_asset"]}\n'
            f'운용 원장 변화: BTC {row["btc_delta"]}, USDT {row["quote_delta"]}\n'
            f'원화는 {_basis(rate_source)} 기준 추정치입니다.')


class DeliveryLedger:
    def __init__(self, directory):
        self.db = sqlite3.connect(Path(directory) / 'telegram.sqlite3', timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS sent('
                        'event_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL)')
        self.db.commit()

    def sent(self, event_id):
        return self.db.execute('SELECT 1 FROM sent WHERE event_id=?', (event_id,)).fetchone() is not None

    def mark(self, event_id, message_id):
        self.db.execute('INSERT OR IGNORE INTO sent VALUES(?,?,?)',
                        (event_id, message_id, int(time.time())))
        self.db.commit()

    def close(self):
        self.db.close()


async def send_message(session, config, body):
    import aiohttp
    url = f'https://api.telegram.org/bot{config.token}/sendMessage'
    try:
        async with session.post(url, json={'chat_id': config.chat_id, 'text': body},
                                allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f'Telegram HTTP {response.status}')
            payload = await response.json(content_type=None)
            if payload.get('ok') is not True or not isinstance(payload.get('result', {}).get('message_id'), int):
                raise RuntimeError('Telegram rejected message')
            return payload['result']['message_id']
    except aiohttp.ClientError:
        # aiohttp exception text may contain the bot token in its URL.
        raise RuntimeError('Telegram transport error') from None


async def dispatch(directory, config, delivery, fx, session):
    state = snapshot(directory)
    now_ms = int(time.time() * 1000)
    if not 0 <= now_ms - state['updated_at_ms'] <= 120_000:
        raise ValueError('Spot heartbeat stale')
    rate = await fx.usd_krw()
    source = fx.last_source
    sent = []
    # A fill always takes priority over the routine daily report.
    for row in fills(directory):
        event_id = f'fill:{row["symbol"]}:{row["trade_id"]}'
        if delivery.sent(event_id):
            continue
        body = fill_text(row, rate, source) + f'\n알림 ID: {event_id}'
        message_id = await send_message(session, config, body)
        delivery.mark(event_id, message_id)
        sent.append(event_id)
        await asyncio.sleep(1)
    day = datetime.fromtimestamp(state['updated_at_ms'] / 1000, KST).strftime('%Y-%m-%d')
    event_id = f'status:{day}'
    if not delivery.sent(event_id):
        body = status_text(state, rate, source) + f'\n알림 ID: {event_id}'
        message_id = await send_message(session, config, body)
        delivery.mark(event_id, message_id)
        sent.append(event_id)
    return sent


async def run(directory, config, *, once=False, interval=30):
    import aiohttp
    directory = Path(directory).resolve()
    lock = InstanceLock(directory / 'telegram.lock')
    lock.acquire()
    delivery = None
    try:
        delivery = DeliveryLedger(directory)
        fx = FxProvider('upbit_usdt', config.fallback_krw, ttl=30)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            while not (directory / 'telegram.stop').exists():
                try:
                    sent = await dispatch(directory, config, delivery, fx, session)
                    print(json.dumps({'status': 'OK', 'sent': sent,
                                      'checked_at': datetime.now(timezone.utc).isoformat()}), flush=True)
                except Exception as exc:
                    print(json.dumps({'status': 'ERROR', **safe_error(exc),
                                      'checked_at': datetime.now(timezone.utc).isoformat()}), flush=True)
                    if once:
                        raise
                if once:
                    break
                await asyncio.sleep(interval)
    finally:
        if delivery is not None:
            delivery.close()
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=state_directory('live'))
    parser.add_argument('--credentials-file', type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    try:
        asyncio.run(run(args.state_dir, settings(args.credentials_file), once=args.once))
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', **safe_error(exc)}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
