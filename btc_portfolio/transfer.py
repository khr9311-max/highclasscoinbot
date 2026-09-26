"""BTC internal transfers with a persisted intent before exactly one POST.

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


async def signed_transfer(gateway, amount, direction="MAIN_CMFUTURE"):
    """Exactly one POST for either BTC Spot/COIN-M transfer direction."""
    amount = number(amount)
    if direction not in {"MAIN_CMFUTURE", "CMFUTURE_MAIN"}:
        raise ValueError("Unsupported internal transfer direction")
    if amount <= 0 or amount.as_tuple().exponent < -8:
        raise ValueError("BTC transfer amount must be positive with at most 8 decimal places")
    server_ms = await gateway._sync_time(force=True)
    params = {"type": direction, "asset": "BTC", "amount": format(amount, "f"),
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


async def transfer_history(gateway, direction, since_ms):
    """Read-only exchange history for human reconciliation of an uncertain intent."""
    if direction not in {"MAIN_CMFUTURE", "CMFUTURE_MAIN"}:
        raise ValueError("Unsupported transfer history direction")
    since_ms = int(since_ms)
    now = await gateway._sync_time(force=True)
    if not 0 < since_ms <= now or now-since_ms > 30*86_400_000:
        raise ValueError("Transfer history start time is outside the supported review window")
    params = {"type": direction, "startTime": max(0, since_ms-60_000),
              "endTime": now, "size": 100, "timestamp": now, "recvWindow": 5000}
    query = urlencode(params)
    signature = hmac.new(gateway._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    response = await gateway.transport.request("GET", BASE_URL+"/sapi/v1/asset/transfer?"+query+"&signature="+signature,
                                               {"X-MBX-APIKEY": gateway._api_key}, 10.0)
    if response.status != 200:
        raise ValueError("Binance transfer history unavailable")
    payload = json.loads(response.text)
    if not isinstance(payload.get("rows"), list):
        raise ValueError("Malformed transfer history")
    return [{"tran_id": row.get("tranId"), "type": row.get("type"), "asset": row.get("asset"),
             "amount": row.get("amount"), "status": row.get("status"), "timestamp": row.get("timestamp")}
            for row in payload["rows"] if row.get("type") == direction and row.get("asset") == "BTC"]


async def rebalance_live(engine, account, markets, fees, difference, month_key):
    """One live service step. PENDING is never resent after any ambiguous POST."""
    from decimal import Decimal as D, ROUND_DOWN

    s, c = engine.s, engine.c
    pending = s.get("aggressive_transfer")
    if pending and pending.get("phase") == "PENDING":
        return {"status": "BLOCKED", "reason": "transfer_outcome_requires_manual_reconciliation"}
    if account["permissions"].get("permitsUniversalTransfer") is not True:
        return {"status": "BLOCKED", "reason": "universal_transfer_permission_required"}
    difference = number(difference)
    wallet = s.get("wallet")
    spot_cash = number(wallet["BTC"])
    if difference > 0:
        direction, amount = "MAIN_CMFUTURE", difference
        if spot_cash < amount:
            attempt_key = "aggressive_rebalance_sells:"+month_key
            if s.get(attempt_key, 0) >= 3:
                return {"status": "BLOCKED", "reason": "rebalance_spot_sell_retry_limit"}
            held = max(c.symbols, key=lambda sym: number(wallet[sym[:-3]])*number(markets["spot"][sym]["bid"]))
            market = markets["spot"][held]
            value = number(wallet[held[:-3]])*number(market["bid"])
            if value <= 0:
                return {"status": "BLOCKED", "reason": "insufficient_owned_spot_btc"}
            target = max(D(0), (value-(amount-spot_cash)*D("1.01"))/(value+spot_cash))
            outcome = await engine.spot_trade(held, target, market, fees["spot"][held], account,
                                              "rebalance_sell:"+month_key+":"+str(s.get(attempt_key, 0)))
            if outcome["status"] == "SUBMITTED":
                s.put(attempt_key, s.get(attempt_key, 0)+1)
            return {"status": "SPOT_SELL_FOR_TRANSFER", "order": outcome}
        if number(next((r["free"] for r in account["spot"]["balances"] if r["asset"] == "BTC"), "0")) < amount:
            return {"status": "BLOCKED", "reason": "spot_btc_not_available"}
    else:
        direction, amount = "CMFUTURE_MAIN", -difference
        asset, pos = account["coin"].asset("BTC"), account["position"]
        market = markets["coinm"]
        exposure = abs(pos.position_amt)*market["spec"].contract_size/number(market["mark"])
        max_by_leverage = number(asset.margin_balance)-exposure/max(D("2.5"), number(c.leverage_max))
        max_by_wallet = number(asset.wallet_balance)-number(asset.position_initial_margin)-D("0.00001")
        amount = min(amount, number(asset.available_balance), max_by_leverage, max_by_wallet)
        if amount <= 0:
            return {"status": "BLOCKED", "reason": "insufficient_coinm_transferable_btc"}
    amount = amount.quantize(D("0.00000001"), rounding=ROUND_DOWN)
    if amount <= 0:
        return {"status": "BLOCKED", "reason": "transfer_below_btc_precision"}
    intent = {"phase": "PENDING", "type": direction, "asset": "BTC", "amount": str(amount),
              "month": month_key, "account_uid_hash": hashlib.sha256(str(account["spot"]["uid"]).encode()).hexdigest(),
              "spot_btc_before": str(spot_cash), "coin_wallet_before": str(account["coin"].asset("BTC").wallet_balance),
              "created_ms": time.time_ns()//1_000_000}
    s.put("aggressive_transfer", intent)
    s.event("aggressive_transfer_intent", intent)
    gateway = next(iter(engine.v.spots.values()))
    try:
        tran_id = await signed_transfer(gateway, amount, direction)
    except Exception as exc:
        s.event("aggressive_transfer_uncertain", {"type": direction, "error_type": type(exc).__name__})
        s.put("halt", "transfer_outcome_requires_manual_reconciliation")
        return {"status": "PENDING", "reason": "transfer_outcome_requires_manual_reconciliation"}
    with s.transaction():
        updated = dict(s.get("wallet"))
        updated["BTC"] = str(number(updated["BTC"])+(-amount if direction == "MAIN_CMFUTURE" else amount))
        intent.update(phase="ACKNOWLEDGED", tran_id=tran_id)
        s.put("wallet", updated)
        s.put("aggressive_transfer", intent)
        s.event("aggressive_transfer_acknowledged", intent)
    return {"status": "ACKNOWLEDGED", "type": direction, "amount_btc": str(amount), "tran_id": tran_id}


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
