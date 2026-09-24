"""Bind one live account to one durable ledger before any engine can run.

Call enforce() under the workspace live-account OS lock. A registry is immutable:
changing an account/path, losing an initialized ledger, or importing an existing
unregistered trading ledger requires explicit offline recovery, never bootstrap.
No credentials or raw account UID are written to the registry or returned report.
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
import uuid


META_KEY = "live_registry_binding"
_KEYS = {"schema_version", "account_uid_hash", "ledger_path", "ledger_uuid"}


class RegistryError(RuntimeError):
    """A safe-to-display binding failure; never includes raw account identity."""


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _uid_hash(account_uid):
    if isinstance(account_uid, bool) or not isinstance(account_uid, (str, int)):
        raise RegistryError("A stable live account UID is required")
    value = str(account_uid)
    if not re.fullmatch(r"[0-9]{1,32}", value) or int(value) <= 0:
        raise RegistryError("A stable live account UID is required")
    value = str(int(value))
    return hashlib.sha256(("btc_spot_live_account_v1:" + value).encode("ascii")).hexdigest()


def _record(value):
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise RegistryError("Live registry record is malformed")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RegistryError("Unsupported live registry version")
    if not isinstance(value["account_uid_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["account_uid_hash"]):
        raise RegistryError("Live registry identity hash is malformed")
    try:
        identifier = str(uuid.UUID(value["ledger_uuid"]))
    except (ValueError, AttributeError, TypeError):
        raise RegistryError("Live registry ledger UUID is malformed") from None
    if identifier != value["ledger_uuid"]:
        raise RegistryError("Live registry ledger UUID is not canonical")
    if not isinstance(value["ledger_path"], str) or _canonical(value["ledger_path"]) != value["ledger_path"]:
        raise RegistryError("Live registry ledger path is not canonical")
    return value


def _read_registry(path):
    try:
        if not path.is_file() or path.stat().st_size > 16_384:
            raise RegistryError("Live registry file is invalid")
        return _record(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        raise RegistryError("Live registry cannot be read safely") from None


def _ledger_snapshot(path):
    """Read all metadata/counts in one SQLite snapshot without creating a DB."""
    connection = None
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise RegistryError("Live ledger is missing or empty")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RegistryError("Live ledger integrity check failed")
        def meta(key):
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        binding, record, wallet, blocker = meta("binding"), meta(META_KEY), meta("wallet"), meta("blocker")
        if not isinstance(binding, dict) or binding.get("mode") != "live" or binding.get("symbol") != "BTCUSDT":
            raise RegistryError("Only a BTCUSDT live ledger can be registered")
        if record is not None:
            _record(record)
        if wallet is not None and not isinstance(wallet, dict):
            raise RegistryError("Live wallet metadata is malformed")
        if wallet is not None:
            for key in ("btc", "quote", "reserve_btc", "reserve_quote"):
                value = wallet.get(key)
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise RegistryError("Live wallet balance metadata is malformed")
                try:
                    if not Decimal(value).is_finite():
                        raise RegistryError("Live wallet balance metadata is malformed")
                except InvalidOperation:
                    raise RegistryError("Live wallet balance metadata is malformed") from None
        if blocker is not None and not isinstance(blocker, dict):
            raise RegistryError("Live blocker metadata is malformed")
        count = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        fills = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        pending = [dict(row) for row in connection.execute(
            "SELECT decision_id,client_id,order_status FROM decisions WHERE phase='PENDING' ORDER BY created_ms,decision_id")]
        return {"binding": binding, "record": record, "wallet_raw": wallet, "blocker_raw": blocker,
                "decision_count": count, "fill_count": fills, "pending": pending,
                "virgin": wallet is None and count == 0 and fills == 0 and blocker is None}
    except RegistryError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise RegistryError("Live ledger cannot be inspected safely") from None
    finally:
        if connection is not None:
            connection.close()


def inspect_binding(account_uid, ledger_path, registry_path):
    """Read-only readiness inspection, including resumable allocation state.

    UNREGISTERED with initialized=False is an acceptable first-run state. Any
    existing registered ledger is checked against both the registry and its DB
    UUID. A missing registry permits only a completely virgin Store database.
    The caller separately checks its requested capital/strategy against returned
    initial_btc/strategy_fingerprint when resuming an existing ledger.
    """
    identity = _uid_hash(account_uid)
    ledger, registry = Path(ledger_path).resolve(), Path(registry_path).resolve()
    canonical = _canonical(ledger)
    if ledger == registry:
        raise RegistryError("Registry and ledger must be separate files")
    registered = registry.exists()
    record = _read_registry(registry) if registered else None
    if record is not None:
        if record["account_uid_hash"] != identity:
            raise RegistryError("Live account differs from the registered account")
        if record["ledger_path"] != canonical:
            raise RegistryError("Live ledger path differs from the registered path")
        if not ledger.is_file():
            raise RegistryError("Registered live ledger is missing; automatic replacement refused")
    snapshot = _ledger_snapshot(ledger) if ledger.exists() else None
    if snapshot is not None:
        saved = snapshot["record"]
        if registered and saved != record:
            raise RegistryError("Live ledger UUID or binding differs from the registry")
        if not registered and not snapshot["virgin"]:
            raise RegistryError("Existing unregistered trading ledger cannot be adopted automatically")
        if saved is not None and (saved["account_uid_hash"] != identity or saved["ledger_path"] != canonical):
            raise RegistryError("Interrupted live binding belongs to another account or ledger path")
        wallet = snapshot["wallet_raw"]
        if wallet is not None and wallet.get("account_uid") is not None and _uid_hash(wallet["account_uid"]) != identity:
            raise RegistryError("Live wallet identity differs from the registered account")
        if record is None:
            record = saved
    else:
        wallet = None
    blocker = snapshot["blocker_raw"] if snapshot else None
    return {"status": "REGISTERED" if registered else "UNREGISTERED", "account_uid_hash": identity,
            "ledger_path": canonical, "ledger_uuid": record["ledger_uuid"] if record else None,
            "initialized": wallet is not None,
            "wallet": {key: wallet[key] for key in ("btc", "quote", "reserve_btc", "reserve_quote")} if wallet else None,
            "pending_count": len(snapshot["pending"]) if snapshot else 0,
            "pending": snapshot["pending"] if snapshot else [],
            "blocked": blocker is not None,
            "blocker": {"reason": blocker.get("reason"), "sticky": blocker.get("sticky") is True} if blocker else None,
            "decision_count": snapshot["decision_count"] if snapshot else 0,
            "fill_count": snapshot["fill_count"] if snapshot else 0,
            "binding": {key: snapshot["binding"].get(key)
                        for key in ("mode", "symbol", "initial_btc", "strategy_fingerprint")} if snapshot else None,
            "initial_btc": snapshot["binding"].get("initial_btc") if snapshot else None,
            "strategy_fingerprint": snapshot["binding"].get("strategy_fingerprint") if snapshot else None}


def _fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_registry(path, record):
    """Publish a fully fsynced file atomically without replacing another binding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard link is atomic and exclusive on NTFS/Linux: no
        # partial JSON is visible and a concurrently created registry survives.
        os.link(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        raise RegistryError("Live registry publication failed; engine must not start") from None
    finally:
        temporary.unlink(missing_ok=True)


def enforce(account_uid, ledger_path, registry_path, store_factory):
    """Return an opened Store only after immutable live registration succeeds.

    The caller must hold the workspace live-account lock for this entire call
    and the resulting engine lifetime. store_factory receives a resolved Path.
    On failure any created Store is closed; no engine or order is started here.
    """
    ledger, registry = Path(ledger_path).resolve(), Path(registry_path).resolve()
    before = inspect_binding(account_uid, ledger, registry)
    store = None
    try:
        store = store_factory(ledger)
        if _canonical(store.path) != _canonical(ledger):
            raise RegistryError("Store factory opened a different ledger path")
        current = _ledger_snapshot(ledger)
        if before["status"] == "REGISTERED":
            # Recheck after Store has acquired its own DB lock. Never silently
            # bless an empty replacement created by a race or accidental loss.
            inspect_binding(account_uid, ledger, registry)
            return store
        if not current["virgin"]:
            raise RegistryError("First registration requires an untouched live ledger")
        record = current["record"]
        if record is None:
            record = {"schema_version": 1, "account_uid_hash": before["account_uid_hash"],
                      "ledger_path": before["ledger_path"], "ledger_uuid": str(uuid.uuid4())}
            with store.transaction():
                if store._get(META_KEY) is not None:
                    raise RegistryError("Live ledger binding changed during registration")
                store._set(META_KEY, record)
        elif record["account_uid_hash"] != before["account_uid_hash"] or record["ledger_path"] != before["ledger_path"]:
            raise RegistryError("Interrupted live binding does not match this request")
        # Crash before publication leaves only a virgin DB with a recoverable
        # UUID; crash after publication leaves a matching durable pair.
        _write_registry(registry, record)
        inspect_binding(account_uid, ledger, registry)
        return store
    except BaseException:
        if store is not None:
            store.close()
        raise
