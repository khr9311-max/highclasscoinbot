import json
import os
import sqlite3

import pytest

from btc_spot.migrate_live import migrate
from btc_spot.registry import enforce, inspect_binding, META_KEY
from btc_spot.runtime import fingerprint
from btc_spot.store import Store


def source(tmp_path):
    old_ledger = tmp_path / 'pc' / 'ledger.sqlite3'
    old_registry = tmp_path / 'pc' / 'live-registry.json'
    factory = lambda path: Store(path, 'live', strategy_fingerprint=fingerprint())
    store = enforce('12345', old_ledger, old_registry, factory)
    try:
        store.initialize_wallet(total_btc='0.004', total_quote='0', account_uid='12345')
        store.create_decision('2026-09-24', '1', phase='NOOP', reason='already_at_target')
    finally:
        store.close()
    return old_ledger, old_registry


def test_offline_migration_preserves_identity_and_history(tmp_path):
    old_ledger, old_registry = source(tmp_path)
    new_ledger = tmp_path / 'aws' / 'ledger.sqlite3'
    new_registry = tmp_path / 'aws-state' / 'live-registry.json'
    original = json.loads(old_registry.read_text())
    check = migrate(old_ledger, old_registry, new_ledger, new_registry)
    assert check['status'] == 'READY' and not new_ledger.exists()
    report = migrate(old_ledger, old_registry, new_ledger, new_registry, apply=True)
    assert report['status'] == 'MIGRATED'
    assert json.loads(old_registry.read_text()) == original
    result = inspect_binding('12345', new_ledger, new_registry)
    assert result['ledger_uuid'] == original['ledger_uuid']
    assert result['decision_count'] == 1 and result['wallet']['btc'] == '0.003'
    assert json.loads(new_registry.read_text())['ledger_path'] == os.path.normcase(str(new_ledger))
    with pytest.raises(ValueError, match='must not exist'):
        migrate(old_ledger, old_registry, new_ledger, new_registry, apply=True)


def test_rejects_mismatched_registry_and_pending_order(tmp_path):
    old_ledger, old_registry = source(tmp_path)
    dest_ledger, dest_registry = tmp_path / 'dest.db', tmp_path / 'dest.json'
    with sqlite3.connect(old_ledger) as db:
        row = db.execute('SELECT value FROM metadata WHERE key=?', (META_KEY,)).fetchone()
        record = json.loads(row[0])
        record['ledger_uuid'] = '00000000-0000-0000-0000-000000000001'
        db.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(record), META_KEY))
    with pytest.raises(ValueError, match='differ'):
        migrate(old_ledger, old_registry, dest_ledger, dest_registry, apply=True)
    assert not dest_ledger.exists()


def test_rejects_pending_order(tmp_path):
    old_ledger, old_registry = source(tmp_path)
    with Store(old_ledger, 'live', strategy_fingerprint=fingerprint()) as store:
        store.create_decision('2026-09-25', '0.5', side='SELL', quantity='0.001')
    with pytest.raises(ValueError, match='pending'):
        migrate(old_ledger, old_registry, tmp_path / 'out.db', tmp_path / 'out.json', apply=True)
