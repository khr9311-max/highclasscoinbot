"""Durable allocation, once-per-day intents and idempotent actual spot fills."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from binance_coinm_v1.runtime.instance_lock import InstanceLock


def number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a financial amount")
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid financial amount") from exc
    if not value.is_finite():
        raise ValueError("Financial amount must be finite")
    return value


def text_number(value):
    return format(number(value).normalize(), "f")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path, mode, initial_btc=Decimal(".003"), *,
                 symbol="BTCUSDT", strategy_fingerprint):
        if mode not in {"paper", "live"} or symbol != "BTCUSDT" or not strategy_fingerprint:
            raise ValueError("Explicit paper/live mode, BTCUSDT and strategy fingerprint are required")
        if number(initial_btc) <= 0:
            raise ValueError("Allocated BTC must be positive")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = InstanceLock(self.path.with_suffix(self.path.suffix + ".lock"))
        self.lock.acquire()
        self.db = None
        self.binding = {"schema_version": 2, "mode": mode, "symbol": symbol,
                        "initial_btc": text_number(initial_btc),
                        "strategy_fingerprint": str(strategy_fingerprint)}
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(
                    decision_id TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL,
                    target TEXT NOT NULL, side TEXT, quantity TEXT,
                    order_type TEXT NOT NULL, limit_price TEXT, time_in_force TEXT NOT NULL,
                    phase TEXT NOT NULL, reason TEXT NOT NULL,
                    order_id TEXT, order_status TEXT,
                    market_json TEXT NOT NULL, created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS fills(
                    symbol TEXT NOT NULL, trade_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
                    order_id TEXT NOT NULL, payload TEXT NOT NULL,
                    btc_delta TEXT NOT NULL, quote_delta TEXT NOT NULL,
                    fee_asset TEXT NOT NULL, fee_amount TEXT NOT NULL,
                    PRIMARY KEY(symbol,trade_id));
            """)
            with self.transaction():
                prior = self._get("binding")
                if prior is None:
                    self._set("binding", self.binding)
                elif prior != self.binding:
                    raise ValueError("State mode, BTC budget, symbol or strategy fingerprint changed")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _get(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set(self, key, value):
        self.db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, encoded(value)))

    def wallet(self):
        return self._get("wallet")

    def initialize_wallet(self, *, total_btc, total_quote, account_uid=None):
        allocated = number(self.binding["initial_btc"])
        if number(total_btc) < allocated or number(total_quote) < 0:
            raise ValueError("Free account BTC does not cover the isolated allocation")
        with self.transaction():
            if self.wallet() is not None:
                raise ValueError("Allocation is already initialized")
            self._set("wallet", {"btc": text_number(allocated), "quote": "0",
                "reserve_btc": text_number(number(total_btc)-allocated),
                "reserve_quote": text_number(total_quote), "account_uid": str(account_uid) if account_uid is not None else None})

    def bind_uid(self, uid):
        if uid is None:
            return
        with self.transaction():
            wallet = self.wallet()
            if wallet is None:
                raise ValueError("No initialized allocation")
            if wallet["account_uid"] is None:
                wallet["account_uid"] = str(uid)
                self._set("wallet", wallet)
            elif wallet["account_uid"] != str(uid):
                raise ValueError("Account UID differs from the state binding")

    def blocker(self):
        return self._get("blocker")

    def set_blocker(self, reason, *, sticky=False, **details):
        existing = self.blocker()
        if existing and existing.get("sticky") and not sticky:
            return existing
        blocker = {"reason": reason, "sticky": bool(sticky), "details": details}
        with self.transaction():
            self._set("blocker", blocker)
        return blocker

    def clear_transient_blocker(self):
        with self.transaction():
            blocker = self.blocker()
            if not blocker or not blocker.get("sticky"):
                self.db.execute("DELETE FROM metadata WHERE key='blocker'")

    def decision(self, decision_id):
        row = self.db.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(row) if row else None

    def pending(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM decisions WHERE phase='PENDING' ORDER BY created_ms,decision_id")]

    def client_id(self, decision_id):
        digest = hashlib.sha256((encoded(self.binding) + "|" + decision_id).encode()).hexdigest()
        return "bsg_" + digest[:28]

    def create_decision(self, decision_id, target, *, side=None, quantity=None,
                        limit_price=None, phase="PENDING", reason="intent_committed", market=None):
        now = time.time_ns() // 1_000_000
        with self.transaction():
            if self.pending():
                raise ValueError("An unresolved earlier intent prevents new decisions")
            self.db.execute("""INSERT INTO decisions(decision_id,client_id,target,side,quantity,order_type,limit_price,
                              time_in_force,phase,reason,market_json,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (decision_id, self.client_id(decision_id), text_number(target), side,
                             text_number(quantity) if quantity is not None else None, "LIMIT",
                             text_number(limit_price) if limit_price is not None else None, "IOC", phase, reason,
                             encoded(market or {}), now, now))
        return self.decision(decision_id)

    def update_order(self, decision_id, *, order_id=None, order_status=None, reason=None, complete=False):
        with self.transaction():
            row = self.decision(decision_id)
            if row is None:
                raise ValueError("Intent must exist before order state")
            if row["order_id"] is not None and order_id is not None and str(order_id) != row["order_id"]:
                raise ValueError("Client intent mapped to different exchange order IDs")
            self.db.execute("""UPDATE decisions SET order_id=?,order_status=?,reason=?,phase=?,updated_ms=?
                              WHERE decision_id=?""", (str(order_id) if order_id is not None else row["order_id"],
                    order_status if order_status is not None else row["order_status"], reason or row["reason"],
                    "COMPLETE" if complete else row["phase"], time.time_ns()//1_000_000, decision_id))

    def apply_fills(self, decision_id, normalized):
        """Insert trades and change allocated balances in one SQLite transaction."""
        applied = 0
        with self.transaction():
            wallet = self.wallet()
            if wallet is None:
                raise ValueError("Cannot apply a fill before allocation initialization")
            btc, quote = number(wallet["btc"]), number(wallet["quote"])
            for trade in normalized:
                payload = encoded(trade)
                old = self.db.execute("SELECT decision_id,payload FROM fills WHERE symbol=? AND trade_id=?",
                                      (self.binding["symbol"], trade["trade_id"])).fetchone()
                if old:
                    if old["decision_id"] != decision_id or old["payload"] != payload:
                        raise ValueError("A previously booked trade changed or was reassigned")
                    continue
                qty, cost, fee = number(trade["quantity"]), number(trade["quote_quantity"]), number(trade["fee"])
                btc_delta = qty if trade["side"] == "BUY" else -qty
                quote_delta = -cost if trade["side"] == "BUY" else cost
                if trade["fee_asset"] == "BTC":
                    btc_delta -= fee
                elif trade["fee_asset"] == "USDT":
                    quote_delta -= fee
                self.db.execute("""INSERT INTO fills(symbol,trade_id,decision_id,order_id,payload,btc_delta,
                                  quote_delta,fee_asset,fee_amount) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (self.binding["symbol"], trade["trade_id"], decision_id, trade["order_id"], payload,
                     text_number(btc_delta), text_number(quote_delta), trade["fee_asset"], text_number(fee)))
                btc += btc_delta
                quote += quote_delta
                applied += 1
            wallet.update(btc=text_number(btc), quote=text_number(quote))
            self._set("wallet", wallet)
        return applied

    def fees(self):
        result = {}
        for row in self.db.execute("SELECT fee_asset,fee_amount FROM fills"):
            result[row[0]] = result.get(row[0], Decimal(0)) + number(row[1])
        return {asset: text_number(value) for asset, value in sorted(result.items())}

    def fill_count(self):
        return self.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]

    def unsupported_fee_assets(self):
        return [asset for asset, value in self.fees().items() if asset not in {"BTC", "USDT"} and number(value) > 0]

    def latest_decision(self):
        row = self.db.execute("SELECT * FROM decisions ORDER BY decision_id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
