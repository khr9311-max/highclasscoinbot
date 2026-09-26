"""Rebind the live ledger to reviewed new code or settings while a position stays open.

The runtime refuses to start when the ledger binding (settings + code hash)
changes. `migration` handles the swing -> aggressive switch and needs a flat
account; this tool is for later code or setting updates. With the trading
service stopped it runs the runtime's own read-only preparation, requires the
binding change to be the only blocker, confirms the ledger's COIN-M quantity and
protective stop against the exchange, backs up the ledger and replaces only the
binding. Positions, stop, wallet, reserve and the kill baseline are untouched.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_spot.config import DEFAULT_CREDENTIALS, credentials
from btc_spot.runtime import safe_error
from btc_spot.store import number

from .config import load
from .migration import service_stopped, snapshot

BINDING_ONLY = {"portfolio_ledger_binding_changed", "explicit_aggressive_ledger_migration_required"}
CONFIRM = "I_UNDERSTAND_CODE_REBIND"


def rebind_blockers(state, account, readiness, stopped):
    """Everything that must hold before only the binding may change."""
    blockers = [b for b in readiness.get("blockers", []) if b not in BINDING_ONLY]
    if not stopped:
        blockers.append("trading_services_must_be_stopped")
    if state["binding"].get("mode") != "live":
        blockers.append("ledger_is_not_live")
    if state.get("halt"):
        blockers.append("existing_halt_requires_resolution")
    amount = account["position"].position_amt
    if number(state.get("coin_qty", "0")) != amount:
        blockers.append("ledger_coinm_quantity_differs_from_exchange")
    if amount:
        stop = (state.get("stop") or {}).get("id")
        if not stop or stop not in {a.client_id for a in account["algos"]}:
            blockers.append("ledger_protective_stop_not_on_exchange")
    return list(dict.fromkeys(blockers))


def rebind(path, expected_old, new_identity, backup_path, detail):
    """Back up the ledger, then replace only the binding in one transaction."""
    backup_path = Path(backup_path)
    if backup_path.exists():
        raise ValueError("A new explicit backup path is required")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
    os.chmod(backup_path, 0o600)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT value FROM state WHERE key='binding'").fetchone()
            previous = json.loads(row[0])
            if previous != {"identity": expected_old, "mode": "live"}:
                raise ValueError("Ledger binding changed before rebind")
            db.execute("UPDATE state SET value=? WHERE key='binding'",
                       (json.dumps({"identity": new_identity, "mode": "live"}),))
            db.execute("INSERT INTO events(kind,payload,created_ms) VALUES(?,?,?)",
                       ("code_rebind", json.dumps({"old_identity": expected_old, "new_identity": new_identity,
                                                   "backup": str(backup_path), **detail}), time.time_ns()//1_000_000))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def identity_files(config):
    """Hashes of the source files in the new identity, recorded with the rebind."""
    root = Path(__file__).resolve().parents[1]
    names = [f"btc_portfolio/{n}" for n in ("signals.py", "engine.py", "store.py", "venues.py", "spot_gateway.py",
                                            "filters.py", "runtime.py", "config.py", "aggressive.py", "transfer.py",
                                            "timing.py")]
    return {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names if (root/n).is_file()}


async def dispatch(args):
    from .runtime import prepare
    from .venues import Venues
    config = load(args.config)
    directory = args.state_dir.resolve()
    path = directory/"ledger.sqlite3"
    venues = Venues(config, credentials(args.credentials_file), allow_orders=False)
    try:
        async def check():
            state = snapshot(path)
            readiness = await prepare(venues, config, directory)
            account = await venues.account()
            old, new = state["binding"]["identity"], config.identity()
            return state, {"ready": False, "old_identity": old, "new_identity": new,
                           "blockers": rebind_blockers(state, account, readiness, service_stopped()),
                           "coinm_contracts": str(account["position"].position_amt),
                           "protective_stop": (state.get("stop") or {}).get("id")}
        state, preview = await check()
        if preview["old_identity"] == preview["new_identity"]:
            return {**preview, "status": "UNCHANGED", "ready": True}
        preview["ready"] = not preview["blockers"]
        if args.action == "prepare":
            return preview
        if args.confirm != CONFIRM:
            raise ValueError("Explicit rebind confirmation flag required")
        if not preview["ready"] or args.expected_old_identity != preview["old_identity"]:
            raise ValueError("Rebind preflight or previous identity mismatch")
        if args.backup_path is None:
            raise ValueError("A new explicit backup path is required")
        lock = InstanceLock(directory/"instance.lock")
        lock.acquire()
        try:
            _, fresh = await check()
            if fresh["blockers"] or fresh["old_identity"] != args.expected_old_identity:
                raise ValueError("Rebind state changed before apply")
            rebind(path, args.expected_old_identity, fresh["new_identity"], args.backup_path,
                   {"files": identity_files(config), "coinm_contracts": fresh["coinm_contracts"]})
        finally:
            lock.release()
        return {"status": "REBOUND", "backup": str(args.backup_path), "old_identity": args.expected_old_identity,
                "new_identity": fresh["new_identity"], "coinm_contracts": fresh["coinm_contracts"]}
    finally:
        await venues.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("prepare", "apply"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--expected-old-identity", default="")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = asyncio.run(dispatch(args))
        print(json.dumps(result, indent=2))
        return 0 if result.get("ready", True) else 2
    except Exception as exc:
        print(json.dumps({"status": "ERROR", **safe_error(exc),
                          **({"reason": str(exc)} if type(exc) is ValueError else {})}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
