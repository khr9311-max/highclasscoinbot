"""
SQLite 상태 저장소 (binance_coinm_v1/state/coinm_v1.sqlite3).

업비트 봇의 state/ 와 무관한 새 DB 다. 한 파일에 paper/testnet/live 기록이 같이
들어가며 모든 조회는 mode 로 거른다 (종이 매매 기록이 실거래로 오인되지 않게).

주문은 거래소로 보내기 '전에' PENDING_SUBMIT 으로 먼저 기록한다(write-ahead).
프로세스가 응답 전에 죽어도 재시작 때 clientOrderId 로 거래소에 조회할 수 있다.

API 키/시크릿은 저장하지 않는다. JSON 칼럼은 전부 Redactor 를 거친다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

from .redact import GLOBAL_REDACTOR, Redactor

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    trade_id          TEXT,
    purpose           TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    is_algo           INTEGER NOT NULL DEFAULT 0,
    quantity          TEXT,
    price             TEXT,
    trigger_price     TEXT,
    reduce_only       INTEGER NOT NULL DEFAULT 0,
    close_position    INTEGER NOT NULL DEFAULT 0,
    working_type      TEXT,
    status            TEXT NOT NULL,
    exchange_order_id TEXT,
    actual_order_id   TEXT,
    executed_qty      TEXT DEFAULT '0',
    avg_price         TEXT,
    mode              TEXT NOT NULL,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    exchange_update_ms INTEGER,
    last_error        TEXT,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_trade ON orders(trade_id);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(mode, status);

CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    exchange_order_id TEXT,
    client_order_id   TEXT,
    trade_id          TEXT,
    side              TEXT,
    price             REAL,
    qty               REAL,
    realized_pnl_btc  REAL,
    commission        REAL,
    commission_asset  TEXT,
    is_maker          INTEGER,
    time_ms           INTEGER,
    source            TEXT,
    mode              TEXT NOT NULL,
    recorded_at       REAL,
    UNIQUE(symbol, exchange_trade_id, mode)
);
CREATE INDEX IF NOT EXISTS ix_fills_trade ON fills(trade_id);

CREATE TABLE IF NOT EXISTS positions (
    trade_id          TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    mode              TEXT NOT NULL,
    direction         INTEGER NOT NULL,
    state             TEXT NOT NULL,
    pattern           TEXT,
    signal_id         INTEGER,
    qty_open          TEXT,
    entry_avg_price   REAL,
    stop_price        REAL,
    realized_pnl_btc  REAL DEFAULT 0,
    unrealized_pnl_btc REAL DEFAULT 0,
    trading_fee_btc   REAL DEFAULT 0,
    funding_fee_btc   REAL DEFAULT 0,
    net_pnl_btc       REAL DEFAULT 0,
    realized_pnl_usd  REAL DEFAULT 0,
    unrealized_pnl_usd REAL DEFAULT 0,
    net_pnl_usd       REAL DEFAULT 0,
    net_pnl_krw       REAL DEFAULT 0,
    opened_at         REAL,
    closed_at         REAL,
    close_reason      TEXT,
    data              TEXT NOT NULL,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_positions_state ON positions(mode, state);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    direction   INTEGER NOT NULL,
    bar_time    REAL NOT NULL,
    close_time  REAL NOT NULL,
    entry       REAL,
    stop        REAL,
    targets     TEXT,
    atr         REAL,
    features    TEXT,
    action      TEXT,
    reason      TEXT,
    trade_id    TEXT,
    mode        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE(symbol, pattern, direction, bar_time, mode)
);

CREATE TABLE IF NOT EXISTS strategy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL, trade_id TEXT, event TEXT NOT NULL, details TEXT
);
CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL, trade_id TEXT NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS funding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, funding_time_ms INTEGER NOT NULL, funding_rate REAL,
    mark_price REAL, position_qty REAL, funding_fee_btc REAL, funding_fee_usd REAL,
    trade_id TEXT, source TEXT NOT NULL, mode TEXT NOT NULL, recorded_at REAL,
    UNIQUE(symbol, funding_time_ms, source, mode)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL,
    wallet_balance_btc REAL, available_balance_btc REAL, equity_btc REAL,
    used_margin_btc REAL, unrealized_pnl_btc REAL,
    mark_price REAL, index_price REAL, usd_krw REAL,
    equity_usd REAL, equity_krw REAL, position_qty REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS validation_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, fingerprint TEXT, passed INTEGER NOT NULL, report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT, updated_at REAL
);
"""

TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "NOT_FOUND",
                           "FINISHED", "NOT_PLACED", "EXPIRED_IN_MATCH"}


class Database:
    def __init__(self, path: str, redactor: Optional[Redactor] = None):
        self.path = path
        self.redactor = redactor or GLOBAL_REDACTOR
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _json(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(self.redactor.obj(value), ensure_ascii=False, default=str)

    @staticmethod
    def _load(value: Optional[str]) -> Any:
        return json.loads(value) if value else None

    # ------------------------------------------------------------------ orders
    def upsert_order(self, rec: Dict[str, Any]) -> None:
        now = time.time()
        cur = self.get_order(rec["client_order_id"])
        if cur:
            from decimal import Decimal
            rec = dict(rec)
            old_qty = Decimal(str(cur.get("executed_qty") or 0))
            new_qty = Decimal(str(rec.get("executed_qty") or 0))
            if "executed_qty" in rec:
                rec["executed_qty"] = max(old_qty, new_qty)
            old_status, new_status = cur.get("status"), rec.get("status")
            rank = {"NEW": 0, "PARTIALLY_FILLED": 1, "TRIGGERING": 1, "TRIGGERED": 2}
            if new_status and (old_status in TERMINAL_ORDER_STATUSES - {"NOT_FOUND", "NOT_PLACED"} and
                               new_status not in TERMINAL_ORDER_STATUSES or
                               rank.get(new_status, 3) < rank.get(old_status, -1)):
                rec.pop("status", None)
            if new_qty < old_qty:
                rec.pop("avg_price", None)
        row = dict(cur or {})
        row.update({k: v for k, v in rec.items() if v is not None or k not in row})
        row.setdefault("created_at", now)
        row["updated_at"] = now
        cols = ("client_order_id", "trade_id", "purpose", "symbol", "side", "order_type",
                "is_algo", "quantity", "price", "trigger_price", "reduce_only",
                "close_position", "working_type", "status", "exchange_order_id",
                "actual_order_id", "executed_qty", "avg_price", "mode", "created_at",
                "updated_at", "exchange_update_ms", "last_error", "raw")
        vals = []
        for c in cols:
            v = row.get(c)
            if c == "raw" and v is not None and not isinstance(v, str):
                v = self._json(v)
            elif c == "last_error" and v is not None:
                v = self.redactor.text(v)
            elif c in ("is_algo", "reduce_only", "close_position"):
                v = int(bool(v))
            elif c in ("quantity", "price", "trigger_price", "executed_qty", "avg_price",
                       "exchange_order_id", "actual_order_id") and v is not None:
                v = str(v)
            vals.append(v)
        self.conn.execute(
            f"INSERT OR REPLACE INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals)

    def get_order(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM orders WHERE client_order_id=?",
                              (client_order_id,)).fetchone()
        return self._order_row(r) if r else None

    def find_order_by_exchange_id(self, exchange_order_id: Any, mode: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute(
            "SELECT * FROM orders WHERE mode=? AND (exchange_order_id=? OR actual_order_id=?)",
            (mode, str(exchange_order_id), str(exchange_order_id))).fetchone()
        return self._order_row(r) if r else None

    def list_orders(self, mode: str, trade_id: Optional[str] = None,
                    statuses: Optional[Iterable[str]] = None,
                    exclude_statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM orders WHERE mode=?"
        args: List[Any] = [mode]
        if trade_id is not None:
            q += " AND trade_id=?"
            args.append(trade_id)
        if statuses:
            st = list(statuses)
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if exclude_statuses:
            st = list(exclude_statuses)
            q += f" AND status NOT IN ({','.join('?' * len(st))})"
            args += st
        q += " ORDER BY created_at"
        return [self._order_row(r) for r in self.conn.execute(q, args).fetchall()]

    def open_local_orders(self, mode: str) -> List[Dict[str, Any]]:
        return self.list_orders(mode, exclude_statuses=TERMINAL_ORDER_STATUSES)

    def _order_row(self, r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["is_algo"] = bool(d["is_algo"])
        d["reduce_only"] = bool(d["reduce_only"])
        d["close_position"] = bool(d["close_position"])
        d["raw"] = self._load(d.get("raw")) if d.get("raw") else None
        return d

    # ------------------------------------------------------------------ fills
    def insert_fill(self, f: Dict[str, Any]) -> bool:
        """중복(같은 거래소 체결 id)이면 False."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fills (symbol, exchange_trade_id, exchange_order_id, "
            "client_order_id, trade_id, side, price, qty, realized_pnl_btc, commission, "
            "commission_asset, is_maker, time_ms, source, mode, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["symbol"], str(f["exchange_trade_id"]), _s(f.get("exchange_order_id")),
             f.get("client_order_id"), f.get("trade_id"), f.get("side"),
             float(f["price"]), float(f["qty"]), float(f.get("realized_pnl_btc") or 0.0),
             float(f.get("commission") or 0.0), f.get("commission_asset"),
             int(bool(f.get("is_maker"))), int(f.get("time_ms") or 0), f.get("source"),
             f["mode"], time.time()))
        if cur.rowcount > 0:
            return True
        if f.get("trade_id"):
            cur = self.conn.execute(
                "UPDATE fills SET trade_id=?, client_order_id=COALESCE(client_order_id, ?) "
                "WHERE symbol=? AND exchange_trade_id=? AND mode=? AND trade_id IS NULL",
                (f["trade_id"], f.get("client_order_id"), f["symbol"],
                 str(f["exchange_trade_id"]), f["mode"]))
            return cur.rowcount > 0
        return False

    def fills_for_trade(self, trade_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM fills WHERE trade_id=? ORDER BY time_ms, id", (trade_id,)).fetchall()]

    def assign_fill_trade(self, fill_row_id: int, trade_id: str) -> None:
        self.conn.execute("UPDATE fills SET trade_id=? WHERE id=? AND trade_id IS NULL",
                          (trade_id, fill_row_id))

    def unassigned_fills(self, mode: str, symbol: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM fills WHERE mode=? AND symbol=? AND trade_id IS NULL ORDER BY time_ms",
            (mode, symbol)).fetchall()]

    # ------------------------------------------------------------------ positions (trades)
    def upsert_position(self, trade: Dict[str, Any]) -> None:
        now = time.time()
        existing = self.conn.execute("SELECT created_at FROM positions WHERE trade_id=?",
                                     (trade["trade_id"],)).fetchone()
        created = existing["created_at"] if existing else now
        acc = trade.get("accounting") or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO positions (trade_id, symbol, mode, direction, state, pattern, "
            "signal_id, qty_open, entry_avg_price, stop_price, realized_pnl_btc, "
            "unrealized_pnl_btc, trading_fee_btc, funding_fee_btc, net_pnl_btc, "
            "realized_pnl_usd, unrealized_pnl_usd, net_pnl_usd, net_pnl_krw, opened_at, "
            "closed_at, close_reason, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade["trade_id"], trade["symbol"], trade["mode"], int(trade["direction"]),
             trade["state"], trade.get("pattern"), trade.get("signal_id"),
             _s(trade.get("qty_open")), trade.get("entry_avg_price"), trade.get("stop_price"),
             acc.get("realized_pnl_btc", 0.0), acc.get("unrealized_pnl_btc", 0.0),
             acc.get("trading_fee_btc", 0.0), acc.get("funding_fee_btc", 0.0),
             acc.get("net_pnl_btc", 0.0), acc.get("realized_pnl_usd", 0.0),
             acc.get("unrealized_pnl_usd", 0.0), acc.get("net_pnl_usd", 0.0),
             acc.get("net_pnl_krw", 0.0), trade.get("opened_at"), trade.get("closed_at"),
             trade.get("close_reason"), self._json(trade), created, now))

    def get_position(self, trade_id: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT data FROM positions WHERE trade_id=?", (trade_id,)).fetchone()
        return self._load(r["data"]) if r else None

    def active_positions(self, mode: str, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT data FROM positions WHERE mode=? AND state != 'CLOSED'"
        args: List[Any] = [mode]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        q += " ORDER BY created_at"
        return [self._load(r["data"]) for r in self.conn.execute(q, args).fetchall()]

    def closed_positions(self, mode: str, since: Optional[float] = None,
                         filled_only: bool = True) -> List[Dict[str, Any]]:
        q = "SELECT data FROM positions WHERE mode=? AND state='CLOSED'"
        args: List[Any] = [mode]
        if since is not None:
            q += " AND closed_at >= ?"
            args.append(since)
        q += " ORDER BY closed_at"
        rows = [self._load(r["data"]) for r in self.conn.execute(q, args).fetchall()]
        if filled_only:
            rows = [r for r in rows if r.get("entry_avg_price")]
        return rows

    # ------------------------------------------------------------------ signals
    def insert_signal(self, sig: Dict[str, Any], mode: str) -> Optional[int]:
        """같은 봉·패턴·방향 신호가 이미 있으면 None (재시작 후 중복 처리 방지)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals (symbol, pattern, direction, bar_time, close_time, "
            "entry, stop, targets, atr, features, action, reason, trade_id, mode, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["symbol"], sig["pattern"], int(sig["direction"]), float(sig["bar_time"]),
             float(sig["close_time"]), sig.get("entry"), sig.get("stop"),
             self._json(sig.get("targets") or []), sig.get("atr"),
             self._json(sig.get("features") or {}), sig.get("action", "detected"),
             sig.get("reason"), sig.get("trade_id"), mode, time.time()))
        return cur.lastrowid if cur.rowcount > 0 else None

    def update_signal(self, signal_id: int, action: str, reason: str,
                      trade_id: Optional[str] = None) -> None:
        self.conn.execute("UPDATE signals SET action=?, reason=?, trade_id=COALESCE(?, trade_id) "
                          "WHERE id=?", (action, reason, trade_id, signal_id))

    def list_signals(self, mode: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM signals WHERE mode=? ORDER BY bar_time DESC LIMIT ?",
                                 (mode, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["targets"] = self._load(d["targets"])
            d["features"] = self._load(d["features"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ events
    def log_transition(self, mode: str, trade_id: str, from_state: Optional[str],
                       to_state: str, reason: str) -> None:
        self.conn.execute("INSERT INTO state_transitions (ts, mode, trade_id, from_state, to_state, "
                          "reason) VALUES (?,?,?,?,?,?)",
                          (time.time(), mode, trade_id, from_state, to_state,
                           self.redactor.text(reason or "")))

    def transitions(self, trade_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM state_transitions WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()]

    def log_event(self, mode: str, event: str, details: Any = None,
                  trade_id: Optional[str] = None) -> None:
        self.conn.execute("INSERT INTO strategy_events (ts, mode, trade_id, event, details) "
                          "VALUES (?,?,?,?,?)",
                          (time.time(), mode, trade_id, event, self._json(details)))

    def events(self, mode: str, trade_id: Optional[str] = None,
               event: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM strategy_events WHERE mode=?", [mode]
        if trade_id:
            q += " AND trade_id=?"
            args.append(trade_id)
        if event:
            q += " AND event=?"
            args.append(event)
        rows = self.conn.execute(q + " ORDER BY id", args).fetchall()
        return [{**dict(r), "details": self._load(r["details"])} for r in rows]

    def log_risk_event(self, mode: str, kind: str, details: Any = None,
                       severity: str = "warning") -> None:
        self.conn.execute("INSERT INTO risk_events (ts, mode, kind, severity, details) "
                          "VALUES (?,?,?,?,?)",
                          (time.time(), mode, kind, severity, self._json(details)))

    def risk_events(self, mode: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM risk_events WHERE mode=?", [mode]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        rows = self.conn.execute(q + " ORDER BY id", args).fetchall()
        return [{**dict(r), "details": self._load(r["details"])} for r in rows]

    def insert_funding_event(self, ev: Dict[str, Any], mode: str) -> bool:
        cur = self.conn.execute(
            "INSERT INTO funding_events (symbol, funding_time_ms, funding_rate, "
            "mark_price, position_qty, funding_fee_btc, funding_fee_usd, trade_id, source, "
            "mode, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, funding_time_ms, source, mode) DO UPDATE SET "
            "trade_id=COALESCE(funding_events.trade_id, excluded.trade_id), "
            "mark_price=excluded.mark_price, funding_fee_usd=excluded.funding_fee_usd, "
            "funding_fee_btc=excluded.funding_fee_btc "
            "WHERE (funding_events.trade_id IS NULL OR excluded.trade_id IS NULL "
            "OR funding_events.trade_id=excluded.trade_id) AND "
            "((funding_events.trade_id IS NULL AND excluded.trade_id IS NOT NULL) OR "
            "funding_events.mark_price IS NOT excluded.mark_price OR "
            "funding_events.funding_fee_usd IS NOT excluded.funding_fee_usd OR "
            "funding_events.funding_fee_btc IS NOT excluded.funding_fee_btc)",
            (ev["symbol"], int(ev["funding_time_ms"]), ev.get("funding_rate"),
             ev.get("mark_price"), ev.get("position_qty"), ev.get("funding_fee_btc"),
             ev.get("funding_fee_usd"), ev.get("trade_id"), ev["source"], mode, time.time()))
        return cur.rowcount > 0

    def funding_events(self, mode: str, trade_id: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM funding_events WHERE mode=?", [mode]
        if trade_id:
            q += " AND trade_id=?"
            args.append(trade_id)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY funding_time_ms", args)]

    def insert_account_snapshot(self, snap: Dict[str, Any], mode: str) -> None:
        cols = ("wallet_balance_btc", "available_balance_btc", "equity_btc", "used_margin_btc",
                "unrealized_pnl_btc", "mark_price", "index_price", "usd_krw", "equity_usd",
                "equity_krw", "position_qty", "source")
        self.conn.execute(
            f"INSERT INTO account_snapshots (ts, mode, {','.join(cols)}) "
            f"VALUES (?,?,{','.join('?' * len(cols))})",
            [snap.get("ts", time.time()), mode] + [snap.get(c) for c in cols])

    def latest_account_snapshot(self, mode: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM account_snapshots WHERE mode=? ORDER BY ts DESC, id DESC "
                              "LIMIT 1", (mode,)).fetchone()
        return dict(r) if r else None

    def account_snapshots(self, mode: str, since: float = 0.0) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM account_snapshots WHERE mode=? AND ts>=? ORDER BY ts", (mode, since))]

    # ------------------------------------------------------------------ validation
    def insert_validation_report(self, report: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO validation_reports (ts, fingerprint, passed, report) VALUES (?,?,?,?)",
            (float(report.get("generated_at", time.time())), report.get("fingerprint"),
             int(bool(report.get("passed"))), self._json(report)))
        return int(cur.lastrowid)

    def latest_validation_report(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT report FROM validation_reports ORDER BY ts DESC, id DESC "
                              "LIMIT 1").fetchone()
        return self._load(r["report"]) if r else None

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return self._load(r["value"]) if r and r["value"] is not None else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
                          (key, self._json(value), time.time()))

    # ------------------------------------------------------------------ 점검용
    def dump_text(self) -> str:
        """DB 전체를 문자열로 (테스트에서 비밀값이 없는지 확인용)."""
        parts = []
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for r in self.conn.execute(f"SELECT * FROM {name}"):
                parts.append(json.dumps([str(x) for x in tuple(r)], ensure_ascii=False))
        return "\n".join(parts)


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)
