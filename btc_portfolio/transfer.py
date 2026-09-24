"""One-time, explicit Spot -> COIN-M BTC funding through Binance universal transfer.

This is deliberately separate from the trading loop. An ambiguous response is
never retried: the intent file must be reconciled with Binance first.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlencode

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_spot.config import DEFAULT_CREDENTIALS, credentials
from btc_spot.gateway import BASE_URL, SpotGateway
from btc_spot.runtime import atomic_json, safe_error
from btc_spot.registry import inspect_binding
from btc_spot.store import number

from .config import ROOT, load
from .runtime import legacy_blockers
from .venues import Venues


def old_spot_reserve(ledger):
    path = Path(ledger)
    if not path.is_file():
        raise ValueError("Stopped BTCUSDT ledger required to preserve the old BTC reserve")
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM decisions WHERE phase='PENDING'").fetchone()[0]:
            raise ValueError("Old trading ledger has an unresolved order")
        row = db.execute("SELECT value FROM metadata WHERE key='wallet'").fetchone()
        if row is None:
            raise ValueError("Old trading ledger is not initialized")
        value = json.loads(row[0])
        return number(value["reserve_btc"])


def plan(config, account, reserve_btc, blockers=()):
    spot = account["spot"]
    permissions = account["permissions"]
    btc = next((r for r in spot["balances"] if r.get("asset") == "BTC"), None)
    if btc is None:
        raise ValueError("Spot BTC balance is missing")
    spot_free, spot_locked = number(btc["free"]), number(btc["locked"])
    futures = account["coin"].asset("BTC")
    coin_wallet, coin_free = number(futures.wallet_balance), number(futures.available_balance)
    target = number(config.coinm_btc)
    amount = target-coin_wallet
    reasons = list(blockers)
    if permissions.get("permitsUniversalTransfer") is not True:
        reasons.append("API_KEY_PERMITS_UNIVERSAL_TRANSFER_DISABLED")
    if spot.get("canTrade") is not True or account["coin"].can_trade is not True:
        reasons.append("ACCOUNT_TRADING_DISABLED")
    if account["spot_orders"] or account["coin_orders"] or account["algos"]:
        reasons.append("OPEN_ORDERS_PRESENT")
    if spot_locked or number(futures.initial_margin) or number(futures.open_order_initial_margin):
        reasons.append("LOCKED_ASSETS_OR_FUTURES_MARGIN")
    if any(p.position_amt for p in account["coin"].positions):
        reasons.append("FUTURES_POSITION_PRESENT")
    if not 0 < amount <= target:
        reasons.append("COINM_WALLET_ALREADY_FUNDED_OR_ABOVE_ALLOCATION")
    remaining = spot_free-amount
    if remaining < number(config.spot_btc)+number(reserve_btc):
        reasons.append("SPOT_BTC_AFTER_TRANSFER_WOULD_CONSUME_ALLOCATION_OR_RESERVE")
    return {"type": "MAIN_CMFUTURE", "asset": "BTC", "amount": str(max(amount, 0)),
            "spot_btc_before": str(spot_free), "coinm_wallet_before": str(coin_wallet),
            "spot_btc_after_estimate": str(remaining), "minimum_spot_btc_after": str(number(config.spot_btc)+number(reserve_btc)),
            "internal_transfer_permission": permissions.get("enableInternalTransfer"),
            "universal_transfer_permission": permissions.get("permitsUniversalTransfer"),
            "ready": not reasons, "blockers": reasons, "orders_submitted": 0, "transfers_submitted": 0}


async def signed_transfer(gateway, amount):
    """Exactly one POST. The only destination/type here is MAIN_CMFUTURE BTC."""
    amount = number(amount)
    if amount <= 0 or amount.as_tuple().exponent < -8:
        raise ValueError("BTC transfer amount must be positive with at most 8 decimal places")
    server_ms = await gateway._sync_time(force=True)
    params = {"type": "MAIN_CMFUTURE", "asset": "BTC", "amount": format(amount, "f"),
              "timestamp": server_ms, "recvWindow": 5000}
    body = urlencode(params)
    signature = hmac.new(gateway._api_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": gateway._api_key, "Content-Type": "application/x-www-form-urlencoded"}
    response = await gateway.transport.request("POST", BASE_URL+"/sapi/v1/asset/transfer",
                                               headers, 10.0, body=body+"&signature="+signature)
    if response.status != 200:
        raise ValueError("Binance universal transfer did not return HTTP 200; outcome requires reconciliation")
    try:
        payload = json.loads(response.text)
        tran_id = payload["tranId"]
        if type(tran_id) is not int or tran_id <= 0:
            raise ValueError("invalid ID")
    except (ValueError, KeyError, TypeError):
        raise ValueError("Binance transfer response has no valid transaction ID; outcome requires reconciliation") from None
    return tran_id


async def dispatch(args):
    config = load(args.config)
    secret = credentials(args.credentials_file)
    venues = Venues(config, secret, allow_orders=False)
    try:
        blockers = legacy_blockers()
        reserve = old_spot_reserve(args.old_spot_ledger)
        account = await venues.account()
        binding = inspect_binding(account["spot"]["uid"], args.old_spot_ledger,
                                  ROOT/"btc_spot/state/live-registry.json")
        if binding["status"] != "REGISTERED" or not binding["initialized"]:
            raise ValueError("Old BTCUSDT live ledger must be registered and initialized")
        if not sys.platform.startswith("linux"):
            blockers.append("TRANSFER_MUST_RUN_ON_AWS_HOST_TO_CHECK_LEGACY_SERVICE")
        preview = plan(config, account, reserve, blockers)
        intent_path = args.state_dir/"transfer-intent.json"
        if intent_path.exists():
            preview["blockers"].append("PREVIOUS_TRANSFER_INTENT_REQUIRES_RECONCILIATION")
            preview["ready"] = False
        if args.action == "prepare":
            return preview
        if args.confirm != "I_UNDERSTAND_BTC_COINM_TRANSFER":
            raise ValueError("Explicit --confirm I_UNDERSTAND_BTC_COINM_TRANSFER required")
        if not preview["ready"]:
            return preview
        # The live bot uses this OS lock on the same host. Keep it through POST.
        lock = InstanceLock(ROOT/"btc_spot/state/live-account.lock")
        lock.acquire()
        try:
            blockers = legacy_blockers()
            account = await venues.account()
            fresh = plan(config, account, reserve, blockers)
            if intent_path.exists() or not fresh["ready"] or fresh["amount"] != preview["amount"]:
                raise ValueError("Transfer plan changed before submission")
            intent = {"status": "PENDING", "type": "MAIN_CMFUTURE", "asset": "BTC",
                      "amount": fresh["amount"], "account_uid_hash": hashlib.sha256(str(account["spot"]["uid"]).encode()).hexdigest(),
                      "created_ms": time.time_ns()//1_000_000}
            atomic_json(intent_path, intent)
            gateway = next(iter(venues.spots.values()))
            tran_id = await signed_transfer(gateway, fresh["amount"])
            intent.update(status="ACKNOWLEDGED", tran_id=tran_id)
            atomic_json(intent_path, intent)
            return {"status": "ACKNOWLEDGED", "tran_id": tran_id, "amount_btc": fresh["amount"],
                    "destination": "COIN-M", "note": "Balances must be checked again before trading"}
        finally:
            lock.release()
    finally:
        await venues.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "transfer"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--state-dir", type=Path, default=ROOT/"btc_portfolio/state/live")
    default_ledger = (Path("/var/lib/btc-spot/live/ledger.sqlite3") if sys.platform.startswith("linux")
                      else ROOT/"btc_spot/state/live/ledger.sqlite3")
    parser.add_argument("--old-spot-ledger", type=Path, default=default_ledger)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = asyncio.run(dispatch(args))
        print(json.dumps(result, indent=2))
        return 0 if result.get("ready", True) else 2
    except Exception as exc:
        print(json.dumps({"status":"ERROR", **safe_error(exc),
                          **({"reason":str(exc)} if type(exc) is ValueError else {})}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
