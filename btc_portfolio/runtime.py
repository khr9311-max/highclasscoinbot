"""Explicit preparation and supervised live runtime; no implicit funding transfers."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from btc_spot.runtime import atomic_json, safe_error
from .config import ROOT
from .engine import Engine, coin_plan
from .signals import alt_signal, coinm_signal
from .store import Store


def legacy_blockers():
    """Check services locally; shared filesystem locks cover manual local starts too."""
    blockers = []
    if sys.platform.startswith("linux"):
        for service in ("spotlive", "coinm-live", "coinbot"):
            check = subprocess.run(["systemctl", "is-active", service], capture_output=True, timeout=5)
            if check.returncode == 0:
                blockers.append("stop_legacy_service:"+service)
    for path in (ROOT/"btc_spot/state/live/status.json", Path("/var/lib/btc-spot/live/status.json")):
        if path.is_file():
            state = json.loads(path.read_text(encoding="utf-8"))
            if time.time()*1000-state.get("updated_at_ms", 0) < 120_000:
                blockers.append("legacy_spot_has_recent_heartbeat")
        ledger = path.parent/"ledger.sqlite3"
        if ledger.exists():
            import sqlite3
            with sqlite3.connect(f"file:{ledger.resolve().as_posix()}?mode=ro", uri=True) as db:
                if db.execute("SELECT COUNT(*) FROM decisions WHERE phase='PENDING'").fetchone()[0]:
                    blockers.append("legacy_spot_unresolved_order")
    return blockers


async def prepare(venues, config, directory):
    markets, account, fees = await venues.markets(), await venues.account(), await venues.fees()
    directory = Path(directory)
    # Preparation must not create a fresh account allocation or modify a live ledger.
    existing = directory/"ledger.sqlite3"
    store = None
    if existing.exists():
        import sqlite3
        class ReadStore:
            def __init__(self):
                self.db = sqlite3.connect(f"file:{existing.resolve().as_posix()}?mode=ro", uri=True)
            def get(self, key, default=None):
                row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else default
        store = ReadStore()
    try:
        engine = Engine(venues, store, config)
        blockers = engine.readiness(account) + legacy_blockers()
        if store and store.get("binding") != {"identity": config.identity(), "mode": "live"}:
            blockers.append("portfolio_ledger_binding_changed")
        if store and store.get("halt"):
            blockers.append("portfolio_halted:"+store.get("halt"))
        coin = coinm_signal(markets["coinm"])
        from btc_spot.store import number
        from .filters import validate_plan_filters, plan_order
        alt = alt_signal(markets["spot"])
        alt_preview = {"status": "SKIP", "reason": "all_alts_underperform_btc"}
        if alt["symbol"]:
            symbol = alt["symbol"]
            market = markets["spot"][symbol]
            wallet = store.get("wallet") if store else None
            btc = number(wallet["BTC"] if wallet else config.spot_btc)
            allocation = min(number(config.spot_btc)*number(config.alt_max_fraction),
                config.total*number(config.risk_fraction)/(number(config.alt_stop_fraction)+2*fees["spot"][symbol]+number(".001")))
            alt_preview = plan_order(btc_balance=wallet[symbol[:-3]] if wallet else 0, quote_balance=btc,
                target_btc_fraction=min(number(1), allocation/btc) if btc else 0, market=market, fee_rate=fees["spot"][symbol])
            market["account_filters"] = await venues.spots[symbol].relevant_filters()
            validate_plan_filters(market, alt_preview, account["spot"], "live")
            alt_preview.update(symbol=symbol, base_asset=symbol[:-3], quote_asset="BTC",
                               scope="entry_sizing_only_current_holdings_and_decision_history_may_prevent_execution")
        result = {"mode": "prepare", "orders_submitted": 0, "identity": config.identity(),
                  "config": asdict(config), "ready": not blockers, "blockers": blockers,
                  "spot_btc_free": str(next((r["free"] for r in account["spot"]["balances"] if r["asset"] == "BTC"), "0")),
                  "coinm_btc_available": str(account["coin"].asset("BTC").available_balance),
                  "coinm_margin_type": account["position"].margin_type,
                  "coinm_leverage": account["position"].leverage,
                  "hedge_mode": account["hedge_mode"],
                  "alt_signal": alt, "alt_order_preview": alt_preview, "coinm_signal": coin,
                  "coinm_order_preview": coin_plan(config, markets["coinm"], coin, fees["coinm"],
                       number(account["coin"].asset("BTC").available_balance), config.total),
                  "updated_at_ms": time.time_ns()//1_000_000}
        atomic_json(directory/"readiness.json", result)
        return result
    finally:
        if store:
            store.db.close()


async def run(venues, config, directory, *, once=False, poll_seconds=30):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    blockers = legacy_blockers()
    if blockers:
        raise ValueError(";".join(blockers))
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    venues.stop_requested = lambda: stop.is_set() or (directory/"stop.request").exists()
    locks = [InstanceLock(ROOT/"btc_spot/state/live-account.lock"),
             InstanceLock(ROOT/"binance_coinm_v1/state/coinm_v1.sqlite3.lock"),
             InstanceLock(ROOT/"btc_portfolio/state/live-account.lock"),
             InstanceLock(directory/"instance.lock")]
    acquired = []
    store = None
    try:
        for lock in locks:
            lock.acquire()
            acquired.append(lock)
        # Pin this account's portfolio ledger path; a different directory cannot reset it.
        await venues.markets()
        account = await venues.account()
        uid = str(account["spot"].get("uid") or "")
        if not uid:
            raise ValueError("Account UID unavailable")
        registry = ROOT/"btc_portfolio/state/live-registry.json"
        import hashlib
        binding = {"uid_hash": hashlib.sha256(uid.encode()).hexdigest(), "ledger": str(directory/"ledger.sqlite3")}
        if registry.exists():
            if json.loads(registry.read_text(encoding="utf-8")) != binding or not (directory/"ledger.sqlite3").exists():
                raise ValueError("Portfolio ledger location changed or missing")
        else:
            blockers = Engine(venues, None, config).readiness(account)
            if blockers:
                raise ValueError(";".join(blockers))
            atomic_json(registry, binding)
        store = Store(directory/"ledger.sqlite3", config.identity(), "live")
        engine = Engine(venues, store, config)
        while not venues.stop_requested():
            try:
                result = await engine.tick()
            except Exception as exc:
                result = {"status": "ERROR", **safe_error(exc)}
                # Only our own fixed validation messages; provider messages are never logged.
                if type(exc) is ValueError:
                    result["reason"] = str(exc)
                store.event("runtime_error", result)
            state = {"mode": "live", "orders_enabled": True, "updated_at_ms": time.time_ns()//1_000_000,
                     "result": result, "wallet": store.get("wallet"), "coin_qty": store.get("coin_qty"),
                     "stop": store.get("stop"), "halt": store.get("halt"), "pending": len(store.pending())}
            atomic_json(directory/"status.json", state)
            print(json.dumps(state, default=str), flush=True)
            if once:
                return state
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        if store:
            store.close()
        for lock in reversed(acquired):
            lock.release()
