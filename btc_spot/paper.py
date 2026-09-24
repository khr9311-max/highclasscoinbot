"""Durable, credential-free simulated spot exchange for forward observations.

Only ``market`` and ``close`` are delegated to the public gateway. Orders
observe a new bid/ask with adverse slippage; LIMIT IOC cannot exceed its bound. There are no
historical fills, borrowing, reserve spending or terminal conversions here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from btc_lab.market_fit import floor_step, market_rules


MAX_MARKET_AGE_MS = 30_000
MAX_CLOCK_AHEAD_MS = 5_000
CLIENT_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,36}\Z")


class PaperOrderRejected(ValueError):
    """A definite, unfilled local rejection; get_order can still recover an old ID."""
    code = -2010
    maybe_sent = False


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("Invalid paper numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid paper numeric value") from None
    if not number.is_finite() or number < 0:
        raise ValueError("Paper amounts must be finite and nonnegative")
    return number


def _text(value):
    return format(value, "f")


def _now_ms():
    return int(time.time() * 1000)


def _limit_rules(filters, limit):
    """LIMIT uses LOT_SIZE and all notional bounds, regardless of market flags."""
    adjusted = []
    price_filter = None
    for original in filters:
        row = deepcopy(original)
        kind = row["filterType"]
        if kind == "MARKET_LOT_SIZE":
            continue
        if kind == "PRICE_FILTER":
            if price_filter is not None:
                raise PaperOrderRejected("Duplicate paper price filter")
            price_filter = row
        if kind == "MIN_NOTIONAL":
            row["applyToMarket"] = True
        if kind == "NOTIONAL":
            row["applyMinToMarket"] = row["applyMaxToMarket"] = True
        adjusted.append(row)
    if price_filter is None:
        raise PaperOrderRejected("LIMIT IOC requires a current price filter")
    tick, low, high = (_decimal(price_filter.get(key, "0")) for key in ("tickSize", "minPrice", "maxPrice"))
    if limit <= 0 or low and limit < low or high and limit > high or tick and floor_step(limit, tick) != limit:
        raise PaperOrderRejected("Paper limit price violates the symbol price filter")
    return market_rules(adjusted)


def _atomic_write(path, value):
    """Publish only a completely flushed state; never fall back to partial JSON."""
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _writer_lock(path):
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("Paper exchange state already has a writer") from None
    return handle


class PaperGateway:
    api_key = ""
    has_credentials = False
    allow_orders = False  # No real exchange orders; local simulation is permitted.

    def __init__(self, public_gateway, state_path, initial_btc=Decimal(".003"),
                 fee_rate=Decimal(".001"), slippage_bps=Decimal("3")):
        if getattr(public_gateway, "has_credentials", None) is not False or getattr(public_gateway, "allow_orders", None) is not False:
            raise ValueError("Paper requires a keyless public gateway with allow_orders=False")
        self.public_gateway = public_gateway
        self.initial_btc, self.fee_rate, self.slippage_bps = map(_decimal, (initial_btc, fee_rate, slippage_bps))
        if self.initial_btc <= 0 or self.fee_rate >= 1 or self.slippage_bps >= 10000:
            raise ValueError("Invalid paper budget, commission or slippage")
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._closed = False
        self._uncertain = False
        self._writer = _writer_lock(self.state_path.with_name(self.state_path.name + ".lock"))
        settings = {"mode": "paper", "symbol": "BTCUSDT", "fee_asset_mode": "received",
                    "initial_btc": _text(self.initial_btc.normalize()),
                    "fee_rate": _text(self.fee_rate.normalize()),
                    "slippage_bps": _text(self.slippage_bps.normalize())}
        self.fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        try:
            if self.state_path.exists():
                if self.state_path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("Paper state exceeds its size limit")
                try:
                    self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    with localcontext() as context:
                        context.prec = 40
                        self._validate_state()
                except (KeyError, TypeError, AttributeError, json.JSONDecodeError):
                    raise ValueError("Paper state is malformed; refusing to reset its wallet") from None
            else:
                self._state = {"version": 1, "fingerprint": self.fingerprint, "settings": settings,
                               "balances": {"BTC": _text(self.initial_btc), "USDT": "0"},
                               "next_order_id": 1, "next_trade_id": 1, "orders": {}, "trades": []}
                _atomic_write(self.state_path, self._state)
        except BaseException:
            self._writer.close()
            raise

    def _validate_state(self):
        state = self._state
        if state["version"] != 1 or state["fingerprint"] != self.fingerprint:
            raise ValueError("Paper state configuration fingerprint does not match")
        expected = hashlib.sha256(json.dumps(state["settings"], sort_keys=True).encode()).hexdigest()
        if expected != self.fingerprint:
            raise ValueError("Paper state settings do not match its fingerprint")
        orders, trades = state["orders"], state["trades"]
        if not isinstance(orders, dict) or not isinstance(trades, list):
            raise ValueError("Paper order/trade journal is incomplete")
        by_id = {}
        for client_id, order in orders.items():
            order_id = order["orderId"]
            if (type(order_id) is not int or order_id < 1 or order_id in by_id
                    or order["clientOrderId"] != client_id or not CLIENT_ID.fullmatch(client_id)
                    or order["symbol"] != "BTCUSDT" or order["side"] not in ("BUY", "SELL")
                    or order["status"] not in ("FILLED", "EXPIRED") or _decimal(order["origQty"]) <= 0):
                raise ValueError("Paper order identity or status is invalid")
            kind, limit = order["type"], _decimal(order["price"])
            if not ((kind == "MARKET" and limit == 0 and order["timeInForce"] == "GTC")
                    or (kind == "LIMIT" and limit > 0 and order["timeInForce"] == "IOC")):
                raise ValueError("Paper order type or limit is invalid")
            if order["status"] == "EXPIRED" and (kind != "LIMIT" or order["fills"]
                    or _decimal(order["executedQty"]) or _decimal(order["cummulativeQuoteQty"])):
                raise ValueError("Expired paper IOC has inconsistent fills")
            by_id[order_id] = order
        if set(by_id) != set(range(1, len(orders) + 1)):
            raise ValueError("Paper order sequence is invalid")
        btc, quote = self.initial_btc, Decimal(0)
        filled_ids = set()
        for expected_id, trade in enumerate(trades, 1):
            if (type(trade["id"]) is not int or trade["id"] != expected_id or type(trade["orderId"]) is not int
                    or trade["orderId"] not in by_id or trade["orderId"] in filled_ids or trade["symbol"] != "BTCUSDT"):
                raise ValueError("Paper trade sequence is invalid")
            order = by_id[trade["orderId"]]
            filled_ids.add(trade["orderId"])
            if order["status"] != "FILLED" or type(trade["isBuyer"]) is not bool:
                raise ValueError("Paper order identity or status is invalid")
            qty, price, notional, commission = map(_decimal, (trade["qty"], trade["price"], trade["quoteQty"], trade["commission"]))
            buy = order["side"] == "BUY"
            expected_fee = qty * self.fee_rate if buy else notional * self.fee_rate
            if (qty <= 0 or price <= 0 or qty * price != notional or commission != expected_fee
                    or trade["isBuyer"] != buy or trade["commissionAsset"] != ("BTC" if buy else "USDT")
                    or _decimal(order["origQty"]) != qty or _decimal(order["executedQty"]) != qty
                    or _decimal(order["cummulativeQuoteQty"]) != notional
                    or order["fills"] != [{"price": trade["price"], "qty": trade["qty"],
                                           "commission": trade["commission"], "commissionAsset": trade["commissionAsset"],
                                           "tradeId": trade["id"]}]):
                raise ValueError("Paper fill accounting is inconsistent")
            if order["type"] == "LIMIT" and (price > _decimal(order["price"]) if buy else price < _decimal(order["price"])):
                raise ValueError("Paper fill violated its persisted limit")
            btc += qty - commission if buy else -qty
            quote += -notional if buy else notional - commission
            if min(btc, quote) < 0:
                raise ValueError("Paper journal would borrow assets")
        if (filled_ids != {order_id for order_id, order in by_id.items() if order["status"] == "FILLED"}
                or _decimal(state["balances"]["BTC"]) != btc or _decimal(state["balances"]["USDT"]) != quote
                or state["next_order_id"] != len(orders) + 1 or state["next_trade_id"] != len(trades) + 1):
            raise ValueError("Paper balances do not reconcile with its journal")

    def _require_open(self):
        if self._closed:
            raise ValueError("Paper gateway is closed")
        if self._uncertain:
            raise ValueError("Paper state persistence is uncertain; restart before continuing")

    async def market(self):
        self._require_open()
        market = deepcopy(await self.public_gateway.market())
        if (market.get("symbol") != "BTCUSDT" or market.get("base_asset") != "BTC"
                or market.get("quote_asset") != "USDT" or market.get("status") != "TRADING"
                or market.get("spot_allowed") is not True):
            raise PaperOrderRejected("BTCUSDT spot market is unavailable")
        now = _now_ms()
        for field in ("received_at_ms", "server_time_ms"):
            observed = market.get(field)
            if type(observed) is not int or not -MAX_CLOCK_AHEAD_MS <= now - observed <= MAX_MARKET_AGE_MS:
                raise PaperOrderRejected("Paper market snapshot is stale or has invalid time")
        bid, ask, reference = map(_decimal, (market["bid"], market["ask"], market["reference_price"]))
        if not 0 < bid <= ask or reference <= 0:
            raise PaperOrderRejected("Paper market has invalid prices")
        market_rules(market["filters"])
        return market

    async def account(self):
        self._require_open()
        async with self._lock:
            return {"canTrade": True, "accountType": "SPOT", "permissions": ["SPOT"],
                    "balances": [{"asset": asset, "free": self._state["balances"][asset], "locked": "0"}
                                 for asset in ("BTC", "USDT")]}

    async def open_orders(self, *, all_symbols=False):
        self._require_open()
        return []

    async def place_order(self, client_id, side, quantity, *, limit_price=None):
        self._require_open()
        if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id) or side not in ("BUY", "SELL"):
            raise PaperOrderRejected("Invalid paper order identity or side")
        qty = _decimal(quantity)
        limit = _decimal(limit_price) if limit_price is not None else None
        async with self._lock:
            if client_id in self._state["orders"]:
                raise PaperOrderRejected("Duplicate paper client order ID")
            snapshot = await self.market()
            rules = market_rules(snapshot["filters"]) if limit is None else _limit_rules(snapshot["filters"], limit)
            reference = _decimal(snapshot["reference_price"]) if limit is None else limit
            depth_key = "ask_qty" if side == "BUY" else "bid_qty"
            insufficient_depth = depth_key in snapshot and qty > _decimal(snapshot[depth_key])
            if limit is None and insufficient_depth:
                raise PaperOrderRejected("Paper quantity exceeds the observed best-book quantity")
            if (qty <= 0 or qty < rules.min_qty or floor_step(qty, rules.step) != qty
                    or rules.max_qty is not None and qty > rules.max_qty
                    or qty * reference < rules.min_notional
                    or rules.max_notional is not None and qty * reference > rules.max_notional):
                raise PaperOrderRejected("Paper quantity violates current market filters")
            with localcontext() as context:
                context.prec = 40
                price = _decimal(snapshot["ask"] if side == "BUY" else snapshot["bid"])
                price *= 1 + self.slippage_bps / 10000 if side == "BUY" else 1 - self.slippage_bps / 10000
                expired = limit is not None and (insufficient_depth or (price > limit if side == "BUY" else price < limit))
                notional = qty * price
                commission = qty * self.fee_rate if side == "BUY" else notional * self.fee_rate
                btc, quote = (_decimal(self._state["balances"][asset]) for asset in ("BTC", "USDT"))
                if side == "SELL" and btc < qty or side == "BUY" and quote < qty * (limit if limit is not None else price):
                    raise PaperOrderRejected("Insufficient simulated free balance")
                if not expired:
                    btc += qty - commission if side == "BUY" else -qty
                    quote += -notional if side == "BUY" else notional - commission
                if min(btc, quote) < 0:
                    raise PaperOrderRejected("Insufficient simulated free balance")
            next_state = deepcopy(self._state)
            order_id, trade_id = next_state["next_order_id"], next_state["next_trade_id"]
            timestamp = _now_ms()
            asset = "BTC" if side == "BUY" else "USDT"
            fill = {"price": _text(price), "qty": _text(qty), "commission": _text(commission),
                    "commissionAsset": asset, "tradeId": trade_id}
            order = {"symbol": "BTCUSDT", "orderId": order_id, "orderListId": -1,
                     "clientOrderId": client_id, "transactTime": timestamp, "time": timestamp,
                     "updateTime": timestamp, "price": _text(limit) if limit is not None else "0", "origQty": _text(qty),
                     "executedQty": "0" if expired else _text(qty), "cummulativeQuoteQty": "0" if expired else _text(notional),
                     "status": "EXPIRED" if expired else "FILLED", "timeInForce": "IOC" if limit is not None else "GTC",
                     "type": "LIMIT" if limit is not None else "MARKET", "side": side,
                     "isWorking": False, "fills": [] if expired else [fill]}
            trade = {"symbol": "BTCUSDT", "id": trade_id, "orderId": order_id,
                     "price": _text(price), "qty": _text(qty), "quoteQty": _text(notional),
                     "commission": _text(commission), "commissionAsset": asset,
                     "time": timestamp, "isBuyer": side == "BUY", "isMaker": False, "isBestMatch": True}
            next_state["balances"] = {"BTC": _text(btc), "USDT": _text(quote)}
            next_state["orders"][client_id] = order
            if not expired:
                next_state["trades"].append(trade)
                next_state["next_trade_id"] += 1
            next_state["next_order_id"] += 1
            try:
                _atomic_write(self.state_path, next_state)
            except BaseException:
                # The atomic replace may have committed even if its acknowledgement
                # failed. Do not permit a second fill from uncertain in-memory state.
                self._uncertain = True
                raise
            self._state = next_state
            return deepcopy(order)

    async def get_order(self, client_id):
        self._require_open()
        async with self._lock:
            return deepcopy(self._state["orders"].get(client_id))

    async def trades(self, order_id):
        self._require_open()
        async with self._lock:
            return deepcopy([trade for trade in self._state["trades"] if trade["orderId"] == order_id])

    async def close(self):
        if self._closed:
            return
        async with self._lock:
            self._closed = True
            try:
                await self.public_gateway.close()
            finally:
                self._writer.close()
