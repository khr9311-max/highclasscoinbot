"""PC runtime: latest daily signal, present-time execution, durable recovery."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from . import STRATEGY_VERSION
from .config import ROOT
from .strategy import signal


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def fingerprint():
    sources = [ROOT/"btc_spot/strategy.py", ROOT/"btc_lab/market_fit.py",
               ROOT/"btc_spot/engine.py", ROOT/"btc_spot/store.py", ROOT/"btc_spot/gateway.py"]
    digest = hashlib.sha256()
    digest.update(STRATEGY_VERSION.encode())
    for path in sources:
        # Git's Windows CRLF conversion must not change a strategy identity.
        digest.update(path.read_text(encoding="utf-8").encode("utf-8"))
    return STRATEGY_VERSION + ":" + digest.hexdigest()[:24]


def safe_error(exc):
    # Provider exceptions may contain request URLs; never serialize str/repr.
    value = {"error_type": type(exc).__name__}
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        value["error_code"] = code
    return value


def balances(account):
    result = {"BTC": {"free": "0", "locked": "0"},
              "USDT": {"free": "0", "locked": "0"}}
    for row in account["balances"]:
        free, locked = Decimal(row["free"]), Decimal(row["locked"])
        if not free.is_finite() or not locked.is_finite() or min(free, locked) < 0:
            raise ValueError("Invalid account balance")
        if row["asset"] in result or free or locked:
            result[row["asset"]] = {"free": str(free), "locked": str(locked)}
    return result


def valuation(ledger, market, fee_rate):
    wallet = ledger.get("wallet")
    if not wallet:
        return None
    btc, quote = Decimal(wallet["btc"]), Decimal(wallet["quote"])
    estimate = btc + quote / Decimal(market["ask"]) * (1-Decimal(fee_rate))
    return {"held_strategy_btc": str(btc), "held_strategy_usdt": str(quote),
            "estimated_strategy_btc_equivalent_after_conversion_fee": str(estimate),
            "reserve_btc": wallet["reserve_btc"], "reserve_usdt": wallet["reserve_quote"],
            "note": "USDT valuation is an estimate, not held BTC; conversion minimums/slippage may leave dust"}


async def private_checks(gateway, capital, *, initial=True):
    account, permissions, orders, fee, filters = await asyncio.gather(
        gateway.account(), gateway.permissions(), gateway.open_orders(all_symbols=True),
        gateway.commission_rate(), gateway.relevant_filters())
    wallet = balances(account)
    reasons = []
    if account.get("canTrade") is not True:
        reasons.append("ACCOUNT_TRADING_DISABLED")
    if permissions.get("enableSpotAndMarginTrading") is not True:
        reasons.append("ENABLE_SPOT_TRADING_PERMISSION")
    if orders:
        reasons.append("OPEN_SPOT_ORDERS")
    if any(Decimal(wallet[asset]["locked"]) for asset in ("BTC", "USDT")):
        reasons.append("BTC_OR_USDT_LOCKED")
    if initial and Decimal(wallet["BTC"]["free"]) < capital:
        reasons.append("INSUFFICIENT_FREE_BTC_FOR_ALLOCATION")
    # The current wallet contains no BNB. Unexpected third-asset commission is
    # still recorded by the engine and blocks subsequent trades for reconciliation.
    return {"blockers": reasons, "balances": wallet, "_account_uid": account.get("uid"),
            "fee_rate": str(fee), "account_filters": filters,
            "permissions": {key: permissions.get(key) for key in
                            ("enableReading", "enableSpotAndMarginTrading", "enableFutures", "enableWithdrawals", "ipRestrict")},
            "open_order_count": len(orders)}


async def prepare(gateway, capital, output):
    from .engine import plan_order, validate_plan_filters
    from .registry import inspect_binding
    checks = await private_checks(gateway, capital, initial=False)
    uid = checks.pop("_account_uid")
    binding = inspect_binding(uid, Path(output).parent/"ledger.sqlite3", ROOT/"btc_spot/state/live-registry.json")
    wallet = binding.get("wallet")
    if not wallet and Decimal(checks["balances"]["BTC"]["free"]) < capital:
        checks["blockers"].append("INSUFFICIENT_FREE_BTC_FOR_ALLOCATION")
    if binding.get("pending_count") or binding.get("blocked"):
        checks["blockers"].append("LIVE_LEDGER_REQUIRES_RECONCILIATION")
    if binding.get("binding") and (binding["strategy_fingerprint"] != fingerprint()
                                    or Decimal(binding["initial_btc"]) != capital):
        checks["blockers"].append("LIVE_LEDGER_STRATEGY_OR_BUDGET_CHANGED")
    if wallet:
        for asset, key, reserved in (("BTC", "btc", "reserve_btc"), ("USDT", "quote", "reserve_quote")):
            actual = sum((Decimal(checks["balances"][asset][kind]) for kind in ("free", "locked")), Decimal(0))
            if abs(actual-Decimal(wallet[key])-Decimal(wallet[reserved])) > Decimal("0.00000001"):
                checks["blockers"].append("LIVE_ACCOUNT_BALANCE_DRIFT")
                break
    market = await gateway.market()
    market["account_filters"] = checks["account_filters"]
    decision = signal(market["klines"], market["server_time_ms"])
    available = Decimal(checks["balances"]["BTC"]["free"])
    if market["status"] != "TRADING" or not market["spot_allowed"]:
        checks["blockers"].append("SPOT_MARKET_UNAVAILABLE")
    preview = plan_order(btc_balance=wallet["btc"] if wallet else capital,
                         quote_balance=wallet["quote"] if wallet else 0,
                         target_btc_fraction=decision["target_btc_fraction"], market=market,
                         fee_rate=checks["fee_rate"])
    account = {"balances": [{"asset": key, **value} for key, value in checks["balances"].items()]}
    try:
        filter_result = validate_plan_filters(market, preview, account, mode="live")
    except ValueError:
        checks["blockers"].append("IOC_PLAN_FILTER_VALIDATION_FAILED")
        filter_result = "INVALID"
    report = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "read_only": True, "orders_submitted": 0, "symbol": "BTCUSDT",
              "strategy": fingerprint(), "allocated_initial_btc": str(capital),
              "reserve_btc_at_snapshot": wallet["reserve_btc"] if wallet else str(max(Decimal(0), available-capital)),
              "decision": decision, "initial_order_preview": preview,
              "ledger_binding": binding, "filter_check": filter_result,
              "preview_scope": "current live allocation if initialized, otherwise initial BTC allocation; current daily decision may already be recorded",
              "ready_for_explicit_live_start": not checks["blockers"], **checks}
    atomic_json(output, report)
    return report


async def run_loop(gateway, directory, mode, capital, *, once=False, poll_seconds=30):
    from .store import Store
    from .engine import Engine
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    locks = [InstanceLock(directory / "instance.lock")]
    if mode == "live":
        # A second state directory must not allow a second live writer on this PC.
        locks.insert(0, InstanceLock(ROOT / "btc_spot/state/live-account.lock"))
    acquired = []
    store = None
    try:
        for lock in locks:
            lock.acquire()
            acquired.append(lock)
        factory = lambda path: Store(path, mode, capital, strategy_fingerprint=fingerprint())
        if mode == "live":
            from .registry import enforce
            gateway.submission_guard = lambda: not (directory/"stop.request").exists()
            account = await gateway.account()
            store = enforce(account.get("uid"), directory/"ledger.sqlite3", ROOT/"btc_spot/state/live-registry.json", factory)
        else:
            store = factory(directory/"ledger.sqlite3")
        engine = Engine(gateway, store, stop_requested=lambda: (directory/"stop.request").exists())
        while not (directory/"stop.request").exists():
            state = {"mode": mode, "orders_enabled": mode == "live",
                     "strategy": store.binding["strategy_fingerprint"], "allocation_initial_btc": str(capital),
                     "updated_at_ms": int(time.time()*1000)}
            try:
                recovery = await engine.recover()
                market = await gateway.market()
                if mode == "paper":
                    market["account_filters"] = []
                decision = signal(market["klines"], market["server_time_ms"])
                checks = None
                if mode == "live":
                    # Reserve/bootstrap validation belongs to the durable engine;
                    # after a sale, free BTC is correctly lower than initial BTC.
                    checks = await private_checks(gateway, capital, initial=False)
                    market["account_filters"] = checks["account_filters"]
                    engine.fee_rate = Decimal(checks["fee_rate"])
                    # Private requests take time: refresh executable public prices.
                    market = {**await gateway.market(), "account_filters": checks["account_filters"]}
                    decision = signal(market["klines"], market["server_time_ms"])
                if (directory/"stop.request").exists():
                    break
                if checks and checks["blockers"]:
                    result = {"status": "BLOCKED", "reason": checks["blockers"]}
                else:
                    result = await engine.execute(decision["decision_id"], Decimal(decision["target_btc_fraction"]), market)
                state.update(decision=decision, result=result, recovery=recovery,
                             market={key: market[key] for key in ("bid", "ask", "server_time_ms")},
                             ledger=engine.status(), status=result.get("status", "UNKNOWN"),
                             valuation=valuation(engine.status(), market, engine.fee_rate))
            except Exception as exc:
                state.update(status="ERROR", **safe_error(exc), ledger=engine.status())
            state["updated_at_ms"] = int(time.time()*1000)
            atomic_json(directory/"status.json", state)
            print(json.dumps(state, ensure_ascii=True, default=str), flush=True)
            if once:
                return state
            # A stop file is observed within a second while idle.
            deadline = time.monotonic() + poll_seconds
            while time.monotonic() < deadline and not (directory/"stop.request").exists():
                await asyncio.sleep(min(1, max(0, deadline-time.monotonic())))
        return {"status": "STOPPED", "mode": mode}
    finally:
        if store is not None:
            store.close()
        for lock in reversed(acquired):
            lock.release()
        await gateway.close()
