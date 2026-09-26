"""One-time stopped-service COIN-M/Spot BTC allocation transfer."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_spot.config import DEFAULT_CREDENTIALS, credentials
from btc_spot.runtime import atomic_json, safe_error
from btc_spot.store import number

from .aggressive import preview as aggressive_preview
from .config import ROOT, load
from .migration import service_stopped, snapshot
from .transfer import signed_transfer, transfer_history
from .venues import Venues


def plan(config, account, markets, state, directory):
    from decimal import Decimal as D, ROUND_DOWN
    from .filters import account_balances
    path = (Path(directory)/"ledger.sqlite3").resolve()
    registry_path = ROOT/"btc_portfolio/state/live-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else None
    uid = str(account["spot"].get("uid") or "")
    blockers = []
    if registry != {"uid_hash": hashlib.sha256(uid.encode()).hexdigest(), "ledger": str(path)} or uid != state["uid"]:
        blockers.append("portfolio_registry_or_account_mismatch")
    if not service_stopped():
        blockers.append("trading_services_must_be_stopped")
    if account["spot_orders"] or account["coin_orders"] or account["algos"] or account["position"].position_amt:
        blockers.append("orders_stops_or_position_must_be_flat")
    if account["permissions"].get("permitsUniversalTransfer") is not True:
        blockers.append("universal_transfer_permission_required")
    if state.get("halt"):
        blockers.append("existing_halt_requires_resolution")
    actual = account_balances(account["spot"])
    for asset, value in state["wallet"].items():
        row = actual.get(asset, {"total": D(0), "locked": D(0)})
        if row["locked"] or abs(row["total"]-number(value)-number(state["reserve"].get(asset, 0))) > D("0.00000001"):
            blockers.append("spot_wallet_or_reserve_drift:"+asset)
    view = aggressive_preview(config, markets, account, state["wallet"])
    difference = number(view["spot_equity_btc"])-number(config.spot_fraction)*(
        number(view["spot_equity_btc"])+number(view["coinm_equity_btc"]))
    direction = "MAIN_CMFUTURE" if difference > 0 else "CMFUTURE_MAIN"
    amount = abs(difference).quantize(D("0.00000001"), rounding=ROUND_DOWN)
    if amount <= 0:
        blockers.append("already_at_target_allocation")
    if direction == "MAIN_CMFUTURE" and amount > number(state["wallet"]["BTC"]):
        blockers.append("sell_owned_alt_before_spot_to_coinm_transfer")
    if direction == "CMFUTURE_MAIN" and amount > number(account["coin"].asset("BTC").available_balance):
        blockers.append("coinm_available_balance_below_requested_transfer")
    return {"ready": not blockers, "blockers": blockers, "type": direction,
            "amount_btc": str(amount), "spot_equity_btc": view["spot_equity_btc"],
            "coinm_equity_btc": view["coinm_equity_btc"], "target_spot_fraction": config.spot_fraction,
            "orders_submitted": 0, "transfers_submitted": 0}


def fresh_plan_is_safe(previous, current):
    """Allow harmless quote drift while rejecting a materially changed transfer."""
    if not current["ready"] or current["type"] != previous["type"]:
        return False
    old_amount = number(previous["amount_btc"])
    new_amount = number(current["amount_btc"])
    tolerance = max(number("0.00000100"), old_amount * number("0.01"))
    return abs(new_amount-old_amount) <= tolerance


async def dispatch(args):
    config = load(args.config)
    if config.strategy_mode != "aggressive":
        raise ValueError("Aggressive configuration required")
    directory = args.state_dir.resolve()
    path = directory/"ledger.sqlite3"
    intent_path = directory/"allocation-transfer-intent.json"
    venues = Venues(config, credentials(args.credentials_file), allow_orders=False)
    try:
        account, markets = await venues.account(), await venues.markets()
        state = snapshot(path)
        if args.action == "history":
            intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.exists() else state.get("aggressive_transfer")
            if not intent or intent.get("phase") != "PENDING":
                raise ValueError("No pending BTC transfer intent exists")
            rows = await transfer_history(next(iter(venues.spots.values())), intent["type"], intent["created_ms"])
            return {"mode": "history", "orders_submitted": 0, "transfers_submitted": 0,
                    "intent": intent, "matching_direction_btc_history": rows,
                    "spot_btc_free": next((r["free"] for r in account["spot"]["balances"] if r["asset"] == "BTC"), "0"),
                    "coinm_wallet_btc": str(account["coin"].asset("BTC").wallet_balance),
                    "note": "Read-only. Compare IDs, amounts, both wallets and ledger manually; this does not retry or adopt a transfer."}
        result = plan(config, account, markets, state, directory)
        if intent_path.exists():
            result["ready"] = False
            result["blockers"].append("existing_transfer_intent_requires_reconciliation")
        if args.action == "prepare":
            return result
        if args.confirm != "I_UNDERSTAND_PORTFOLIO_BTC_TRANSFER":
            raise ValueError("Explicit internal BTC transfer confirmation flag required")
        if not result["ready"]:
            return result
        lock = InstanceLock(directory/"instance.lock")
        lock.acquire()
        try:
            account, markets = await venues.account(), await venues.markets()
            state = snapshot(path)
            fresh = plan(config, account, markets, state, directory)
            if not fresh_plan_is_safe(result, fresh) or intent_path.exists():
                raise ValueError("Internal transfer plan changed")
            # Use the newest fully validated amount. Spot valuation can move by a
            # satoshi between the two authenticated snapshots.
            result = fresh
            intent = {"phase": "PENDING", "type": result["type"], "asset": "BTC", "amount": result["amount_btc"],
                      "old_identity": state["binding"]["identity"], "created_ms": time.time_ns()//1_000_000}
            atomic_json(intent_path, intent)
            try:
                tran_id = await signed_transfer(next(iter(venues.spots.values())), result["amount_btc"], result["type"])
            except Exception:
                # The request may have reached Binance. Preserve PENDING and do not retry.
                raise ValueError("Transfer outcome unknown; compare Binance history and both wallets manually") from None
            with sqlite3.connect(path, isolation_level=None) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute("SELECT value FROM state WHERE key='binding'").fetchone()
                    wallet_row = db.execute("SELECT value FROM state WHERE key='wallet'").fetchone()
                    if json.loads(row[0])["identity"] != intent["old_identity"] or json.loads(wallet_row[0]) != state["wallet"]:
                        raise ValueError("Ledger changed after transfer submission; manual reconciliation required")
                    wallet = dict(state["wallet"])
                    change = number(result["amount_btc"])*(1 if result["type"] == "CMFUTURE_MAIN" else -1)
                    wallet["BTC"] = str(number(wallet["BTC"])+change)
                    db.execute("UPDATE state SET value=? WHERE key='wallet'", (json.dumps(wallet),))
                    db.execute("INSERT INTO events(kind,payload,created_ms) VALUES(?,?,?)",
                               ("allocation_transfer", json.dumps({"type": result["type"], "amount": result["amount_btc"],
                                "tran_id": tran_id}), time.time_ns()//1_000_000))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            intent.update(phase="ACKNOWLEDGED", tran_id=tran_id)
            atomic_json(intent_path, intent)
            return {"status": "ACKNOWLEDGED", "tran_id": tran_id, "type": result["type"],
                    "amount_btc": result["amount_btc"], "ledger_recorded": True}
        finally:
            lock.release()
    finally:
        await venues.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "transfer", "history"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--state-dir", type=Path, required=True)
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
