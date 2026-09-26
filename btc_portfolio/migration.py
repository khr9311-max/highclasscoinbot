"""Explicit in-place swing -> aggressive ledger migration; never resets the registry."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_spot.config import DEFAULT_CREDENTIALS, credentials
from btc_spot.runtime import safe_error
from btc_spot.store import number

from .config import ROOT, load
from .filters import account_balances
from .venues import Venues


def snapshot(path):
    if not Path(path).is_file():
        raise ValueError("Registered portfolio ledger is missing")
    with sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Portfolio ledger integrity check failed")
        if db.execute("SELECT COUNT(*) FROM intents WHERE phase='PENDING'").fetchone()[0]:
            raise ValueError("Portfolio ledger has an unresolved order")
        state = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM state")}
    if not state.get("binding") or not state.get("wallet") or not state.get("reserve") or not state.get("uid"):
        raise ValueError("Portfolio ledger is not initialized")
    return state


def service_stopped():
    if sys.platform.startswith("linux"):
        for name in ("btc-portfolio", "spotlive", "coinm-live", "coinbot"):
            if subprocess.run(["systemctl", "is-active", name], capture_output=True, timeout=5).returncode == 0:
                return False
    return True


def plan_snapshot(config, directory, state, account):
    path = (Path(directory)/"ledger.sqlite3").resolve()
    registry_path = ROOT/"btc_portfolio/state/live-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else None
    uid = str(account["spot"].get("uid") or "")
    expected = {"uid_hash": hashlib.sha256(uid.encode()).hexdigest(), "ledger": str(path)}
    blockers = []
    if registry != expected or uid != state["uid"]:
        blockers.append("registry_or_account_mismatch")
    if state["binding"].get("mode") != "live":
        blockers.append("ledger_is_not_live")
    if state.get("halt"):
        blockers.append("existing_halt_requires_resolution")
    if not service_stopped():
        blockers.append("trading_services_must_be_stopped")
    if account["spot_orders"] or account["coin_orders"]:
        blockers.append("open_orders_present")
    if account["position"].position_amt:
        blockers.append("flatten_coinm_position_and_reconcile_old_stop_first")
    if account["algos"]:
        blockers.append("cancel_or_reconcile_existing_protective_order_first")
    if number(state.get("coin_qty", 0)) or state.get("stop") or state.get("position_stop"):
        blockers.append("local_coinm_position_or_stop_requires_swing_reconciliation")
    if account.get("spot_bnb_burn") is not False:
        blockers.append("disable_spot_bnb_fee_payment")
    if state.get("aggressive_transfer", {}).get("phase") == "PENDING":
        blockers.append("unresolved_transfer_intent")
    if number(state["wallet"].get("BTC", "0")) < 0:
        blockers.append("invalid_owned_spot_btc")
    expected_assets = {"BTC", *(symbol[:-3] for symbol in config.symbols)}
    if set(state["wallet"]) != expected_assets or set(state.get("reserve", {})) != expected_assets:
        blockers.append("ledger_must_track_all_aggressive_spot_assets")
    actual = account_balances(account["spot"])
    for asset, value in state["wallet"].items():
        row = actual.get(asset, {"total": 0, "locked": 0})
        if row["locked"] or abs(row["total"]-number(value)-number(state.get("reserve", {}).get(asset, 0))) > number("0.00000001"):
            blockers.append("spot_wallet_or_reserve_drift:"+asset)
    if config.rebalance_mode == "auto" and account["permissions"].get("permitsUniversalTransfer") is not True:
        blockers.append("auto_rebalance_needs_universal_transfer_permission")
    return {"ready": not blockers, "blockers": blockers, "old_identity": state["binding"]["identity"],
            "new_identity": config.identity(), "ledger": str(path), "registry_unchanged": True,
            "wallet_unchanged": True, "current_alt_wallet": state["wallet"], "current_coin_contracts": str(account["position"].position_amt)}


async def dispatch(args):
    config = load(args.config)
    if config.strategy_mode != "aggressive":
        raise ValueError("Aggressive configuration required")
    directory = args.state_dir.resolve()
    path = directory/"ledger.sqlite3"
    venues = Venues(config, credentials(args.credentials_file), allow_orders=False)
    try:
        account = await venues.account()
        state = snapshot(path)
        preview = plan_snapshot(config, directory, state, account)
        # Current exchange account shape, mode and leverage are checked again.
        if (number(config.spot_fraction) < 1 and
                (account["position"].margin_type != "isolated" or account["position"].leverage != 3 or account["hedge_mode"])):
            preview["blockers"].append("coinm_requires_isolated_one_way_exchange_leverage_3")
            preview["ready"] = False
        markets = await venues.markets()
        from .aggressive import preview as aggressive_preview
        view = aggressive_preview(config, markets, account, state["wallet"])
        preview["aggressive_preview"] = view
        preview["initial_equity_btc"] = str(number(view["spot_equity_btc"])+number(view["coinm_equity_btc"]))
        if args.action == "prepare":
            return preview
        if args.confirm != "I_UNDERSTAND_AGGRESSIVE_LEDGER_MIGRATION":
            raise ValueError("Explicit migration confirmation flag required")
        if not preview["ready"] or args.expected_old_identity != preview["old_identity"]:
            raise ValueError("Migration preflight or previous identity mismatch")
        backup_path = args.backup_path
        if backup_path is None or backup_path.exists():
            raise ValueError("A new explicit backup path is required")
        lock = InstanceLock(directory/"instance.lock")
        lock.acquire()
        try:
            account = await venues.account()
            state = snapshot(path)
            fresh = plan_snapshot(config, directory, state, account)
            if not fresh["ready"] or fresh["old_identity"] != args.expected_old_identity:
                raise ValueError("Migration state changed before apply")
            markets = await venues.markets()
            from .aggressive import preview as aggressive_preview
            current = aggressive_preview(config, markets, account, state["wallet"])
            fresh["initial_equity_btc"] = str(number(current["spot_equity_btc"])+number(current["coinm_equity_btc"]))
            if number(fresh["initial_equity_btc"]) <= 0:
                raise ValueError("Cannot establish aggressive BTC equity baseline")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as source, sqlite3.connect(backup_path) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
            os.chmod(backup_path, 0o600)
            with sqlite3.connect(path, isolation_level=None) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    previous = db.execute("SELECT value FROM state WHERE key='binding'").fetchone()
                    if json.loads(previous[0])["identity"] != args.expected_old_identity:
                        raise ValueError("Ledger binding changed during migration")
                    db.execute("UPDATE state SET value=? WHERE key='binding'", (json.dumps({"identity": config.identity(), "mode": "live"}),))
                    db.execute("INSERT OR REPLACE INTO state VALUES('aggressive_initial_equity',?)",
                               (json.dumps(fresh["initial_equity_btc"]),))
                    db.execute("INSERT INTO events(kind,payload,created_ms) VALUES(?,?,?)",
                               ("aggressive_migration", json.dumps({"old_identity": args.expected_old_identity,
                                "new_identity": config.identity(), "backup": str(backup_path)}), time.time_ns()//1_000_000))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            return {"status": "MIGRATED", "backup": str(backup_path), "ledger": str(path),
                    "registry_unchanged": True, "wallet_unchanged": True}
        finally:
            lock.release()
    finally:
        await venues.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
