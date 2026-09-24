import asyncio
import json
import sqlite3
import time

import pytest

from btc_spot import notify


def state():
    return {'mode': 'live', 'orders_enabled': True, 'updated_at_ms': int(time.time()*1000),
            'status': 'ALREADY_DECIDED', 'decision': {'target_btc_fraction': '1'},
            'market': {'bid': '100000', 'ask': '100001'},
            'ledger': {'wallet': {'btc': '0.003', 'quote': '0', 'reserve_btc': '0.001',
                                  'reserve_quote': '0'}, 'fill_count': 1, 'pending': []}}


def test_status_and_fill_use_separate_allocated_and_reserve_values():
    body = notify.status_text(state(), 1400, 'upbit_usdt')
    assert '운용 평가: BTC 약 0.00300000 / 약 420,000원' in body
    assert '전체 평가: BTC 약 0.00400000 / 약 560,000원' in body
    row = {'decision_id': '2026-09-24', 'trade_id': '12', 'btc_delta': '-0.001',
           'quote_delta': '100', 'payload': json.dumps({'side': 'SELL', 'quantity': '0.001',
           'quote_quantity': '100', 'price': '100000', 'fee': '0.1', 'fee_asset': 'USDT'})}
    fill = notify.fill_text(row, 1400, 'fixed_fallback')
    assert '매도 체결' in fill and '약 140,000원' in fill and '설정 환율 대체값' in fill


def test_dispatch_deduplicates_after_restart_and_retries_after_failure(tmp_path, monkeypatch):
    asyncio.run(_dispatch_scenario(tmp_path, monkeypatch))


async def _dispatch_scenario(tmp_path, monkeypatch):
    (tmp_path / 'status.json').write_text(json.dumps(state()), encoding='utf-8')
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as db:
        db.executescript('CREATE TABLE metadata(key TEXT,value TEXT);'
                         'CREATE TABLE fills(symbol TEXT,trade_id TEXT,decision_id TEXT,payload TEXT,'
                         'btc_delta TEXT,quote_delta TEXT);')
        db.execute('INSERT INTO metadata VALUES(?,?)', ('binding', json.dumps({'mode': 'live'})))
        db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?)', ('BTCUSDT', '12', '2026-09-24',
            json.dumps({'side': 'SELL', 'quantity': '0.001', 'quote_quantity': '100',
                        'price': '100000', 'fee': '0.1', 'fee_asset': 'USDT'}), '-0.001', '99.9'))
    class FX:
        last_source = 'upbit_usdt'
        async def usd_krw(self):
            return 1400
    calls = []
    async def sender(_session, _config, body):
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError('temporary failure')
        return len(calls)
    async def no_sleep(_):
        pass
    monkeypatch.setattr(notify, 'send_message', sender)
    monkeypatch.setattr(notify.asyncio, 'sleep', no_sleep)
    ledger = notify.DeliveryLedger(tmp_path)
    config = notify.Settings('token', 'chat')
    try:
        with pytest.raises(RuntimeError):
            await notify.dispatch(tmp_path, config, ledger, FX(), None)
        assert not ledger.sent('fill:BTCUSDT:12')
        assert len(await notify.dispatch(tmp_path, config, ledger, FX(), None)) == 2
    finally:
        ledger.close()
    ledger = notify.DeliveryLedger(tmp_path)
    try:
        assert await notify.dispatch(tmp_path, config, ledger, FX(), None) == []
    finally:
        ledger.close()
    assert len(calls) == 3


def test_rejects_paper_snapshot(tmp_path):
    item = state()
    item['mode'] = 'paper'
    (tmp_path / 'status.json').write_text(json.dumps(item), encoding='utf-8')
    with pytest.raises(ValueError, match='live Spot'):
        notify.snapshot(tmp_path)
