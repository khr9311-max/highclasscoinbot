"""Offline, copy-only rebind of a stopped Windows Spot live ledger for Linux.

Run on the destination host against an immutable copy of the stopped PC state.
The original registry and ledger are never modified or deleted.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import uuid

from .runtime import fingerprint, safe_error

META_KEY = 'live_registry_binding'


def _json_meta(db, key):
    row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    if row is None:
        raise ValueError(f'Missing {key} metadata')
    return json.loads(row[0])


def inspect_source(ledger, registry):
    ledger, registry = Path(ledger).resolve(), Path(registry).resolve()
    if not ledger.is_file() or not registry.is_file():
        raise ValueError('Both stopped-PC ledger and registry copies are required')
    if registry.stat().st_size > 16_384:
        raise ValueError('Registry is too large')
    record = json.loads(registry.read_text(encoding='utf-8'))
    if (set(record) != {'schema_version', 'account_uid_hash', 'ledger_path', 'ledger_uuid'}
            or record['schema_version'] != 1
            or not re.fullmatch(r'[0-9a-f]{64}', record['account_uid_hash'])
            or str(uuid.UUID(record['ledger_uuid'])) != record['ledger_uuid']
            or not PureWindowsPath(record['ledger_path']).is_absolute()):
        raise ValueError('Invalid source live registry')
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Source ledger integrity check failed')
        if _json_meta(db, META_KEY) != record:
            raise ValueError('Source ledger UUID and registry differ')
        binding = _json_meta(db, 'binding')
        wallet = _json_meta(db, 'wallet')
        if (binding.get('mode') != 'live' or binding.get('symbol') != 'BTCUSDT'
                or binding.get('strategy_fingerprint') != fingerprint()):
            raise ValueError('Source live strategy differs from deployed source')
        uid = wallet.get('account_uid')
        if not isinstance(uid, str) or not re.fullmatch(r'[0-9]{1,32}', uid) or int(uid) <= 0:
            raise ValueError('Source account identity is missing')
        digest = hashlib.sha256(('btc_spot_live_account_v1:' + str(int(uid))).encode('ascii')).hexdigest()
        if digest != record['account_uid_hash']:
            raise ValueError('Source account identity differs from registry')
        if db.execute("SELECT COUNT(*) FROM decisions WHERE phase='PENDING'").fetchone()[0]:
            raise ValueError('Resolve pending live order before migration')
        if db.execute("SELECT COUNT(*) FROM metadata WHERE key='blocker'").fetchone()[0]:
            raise ValueError('Resolve live blocker before migration')
        decisions = db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
        fills = db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
    return {'record': record, 'uid': uid, 'binding': binding,
            'decision_count': decisions, 'fill_count': fills}


def migrate(source_ledger, source_registry, dest_ledger, dest_registry, *, apply=False):
    source_ledger, source_registry = Path(source_ledger).resolve(), Path(source_registry).resolve()
    dest_ledger, dest_registry = Path(dest_ledger).resolve(), Path(dest_registry).resolve()
    if len({source_ledger, source_registry, dest_ledger, dest_registry}) != 4:
        raise ValueError('All four migration paths must differ')
    info = inspect_source(source_ledger, source_registry)
    if dest_ledger.exists() or dest_registry.exists():
        raise ValueError('Destination live ledger and registry must not exist')
    result = {'status': 'READY' if not apply else 'MIGRATED',
              'source_ledger_uuid': info['record']['ledger_uuid'],
              'decision_count': info['decision_count'], 'fill_count': info['fill_count'],
              'initial_btc': info['binding']['initial_btc'],
              'destination_ledger': str(dest_ledger), 'destination_registry': str(dest_registry)}
    if not apply:
        return result
    dest_ledger.parent.mkdir(parents=True, exist_ok=True)
    dest_registry.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest_ledger.with_name(dest_ledger.name + '.migration-' + uuid.uuid4().hex)
    target_record = {**info['record'], 'ledger_path': os.path.normcase(str(dest_ledger))}
    try:
        with closing(sqlite3.connect(source_ledger.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                target.execute('UPDATE metadata SET value=? WHERE key=?',
                               (json.dumps(target_record, sort_keys=True, separators=(',', ':')), META_KEY))
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Copied ledger integrity check failed')
                target.commit()
        with open(temporary, 'rb+') as stream:
            os.fsync(stream.fileno())
        # Exclusive links prevent accidental replacement of any live state.
        os.link(temporary, dest_ledger)
        from .registry import _write_registry, inspect_binding
        _write_registry(dest_registry, target_record)
        verified = inspect_binding(info['uid'], dest_ledger, dest_registry)
        if (verified['ledger_uuid'] != info['record']['ledger_uuid']
                or verified['decision_count'] != info['decision_count']
                or verified['fill_count'] != info['fill_count']):
            raise ValueError('Migrated live state verification failed')
        return result
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-ledger', type=Path, required=True)
    parser.add_argument('--source-registry', type=Path, required=True)
    parser.add_argument('--dest-ledger', type=Path, required=True)
    parser.add_argument('--dest-registry', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Write new destination files; default is read-only')
    args = parser.parse_args()
    try:
        report = migrate(args.source_ledger, args.source_registry, args.dest_ledger,
                         args.dest_registry, apply=args.apply)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', **safe_error(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
