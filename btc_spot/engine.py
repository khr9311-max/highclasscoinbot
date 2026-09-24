"""Once-daily BTCUSDT allocation execution with durable actual-fill accounting.

No credential loading, transfers, automatic liquidation or intent resubmission.
The gateway supplies public/private reads and owns the order-permission guard.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import time

from btc_lab.market_fit import floor_step, size_spot_order
from .store import Store, encoded, number, text_number

BTC_TOL = Decimal("0.00000001")
QUOTE_TOL = Decimal("0.00000001")
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
ORDER_STATES = TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_NEW", "PENDING_CANCEL"}


def plan_order(*, btc_balance, quote_balance, target_btc_fraction, market,
               fee_rate=Decimal(".001")):
    """Pure IOC budget preview shared by read-only preparation and execution.

    Prices allow three bps plus at most one tick. Both potential BTC and quote
    commissions are budgeted conservatively; actual fees come only from fills.
    Private-account filters and state reconciliation remain execution gates.
    """
    btc, quote, target, fee = map(number, (btc_balance, quote_balance, target_btc_fraction, fee_rate))
    if min(btc, quote) < 0 or not 0 <= target <= 1 or not 0 <= fee < 1:
        raise ValueError("Invalid allocation, target or fee")
    price_rules = [row for row in market["filters"] if row.get("filterType") == "PRICE_FILTER"]
    lot_rules = [row for row in market["filters"] if row.get("filterType") == "LOT_SIZE"]
    if len(price_rules) != 1 or len(lot_rules) != 1:
        raise ValueError("LIMIT IOC needs unique PRICE_FILTER and LOT_SIZE")
    tick, step = number(price_rules[0]["tickSize"]), number(lot_rules[0]["stepSize"])
    if tick <= 0 or step <= 0:
        raise ValueError("LIMIT IOC tick and lot step must be enabled")
    mid = (number(market["bid"])+number(market["ask"]))/2
    wealth = btc*mid + quote
    buying = target*wealth > btc*mid
    raw_price = number(market["ask"])*Decimal("1.0003") if buying else number(market["bid"])*Decimal(".9997")
    limit_price = (raw_price/tick).to_integral_value(rounding=ROUND_CEILING if buying else ROUND_FLOOR)*tick
    if limit_price <= 0:
        raise ValueError("IOC price rounds to zero")
    helper_filters = []
    for source_filter in market["filters"]:
        row = dict(source_filter)
        if row.get("filterType") == "MARKET_LOT_SIZE":
            continue
        if row.get("filterType") == "MIN_NOTIONAL":
            row["applyToMarket"] = True
        elif row.get("filterType") == "NOTIONAL":
            row["applyMinToMarket"] = row["applyMaxToMarket"] = True
        helper_filters.append(row)
    sizing = size_spot_order(btc_balance=btc, quote_balance=quote,
        target_btc_fraction=target, bid=market["bid"], ask=market["ask"], filters=helper_filters,
        fee_rate=fee, slippage_bps=Decimal("3"), reference_price=limit_price, fee_asset_mode="quote")
    sizing.update(order_type="LIMIT", time_in_force="IOC", limit_price=text_number(limit_price),
                  budget_fee_rate=text_number(fee), price_protection="3bps_plus_at_most_one_tick")
    if sizing["status"] == "READY":
        fee_capacity = quote/(limit_price*(1+fee)) if sizing["side"] == "BUY" else btc/(1+fee)
        quantity = floor_step(min(number(sizing["quantity"]), fee_capacity), step)
        minimum_notional = max((number(row.get("minNotional", row.get("notional", "0")))
                               for row in helper_filters if row.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}), default=Decimal(0))
        if quantity < number(lot_rules[0]["minQty"]) or quantity <= 0 or quantity*limit_price < minimum_notional:
            sizing.update(status="SKIP", reason="below_limit_minimum_after_fee_reserve")
        sizing["quantity"] = text_number(quantity)
        # The pure preview is a budget bound, not a fabricated fill result.
        sizing["maximum_quote_spend_including_fee"] = text_number(quantity*limit_price*(1+fee)) if sizing["side"] == "BUY" else "0"
        sizing["maximum_btc_debit_including_fee"] = text_number(quantity*(1+fee)) if sizing["side"] == "SELL" else "0"
    # The research helper estimates a MARKET fill. Its post-trade balances do
    # not describe a capped IOC order, which can expire or fill only in part.
    for key in ("execution_price", "notional", "fee_quote", "fee_btc", "fee_quote_equivalent",
                "quote_delta", "btc_delta", "btc_after", "quote_after", "fee_asset_assumption"):
        sizing.pop(key, None)
    sizing["fee_budget_assumption"] = "BTC_or_USDT_worst_case"
    return sizing


def validate_plan_filters(market, sized, balances, mode="live"):
    if isinstance(balances, dict) and isinstance(balances.get("balances"), list):
        balances = Engine._balances(balances)
    raw = market.get("account_filters")
    if raw is None:
        raise ValueError("Account-specific filter snapshot is required")
    if isinstance(raw, list):
        if raw:
            raise ValueError("Account filter categories must be explicit")
        if mode == "live":
            raise ValueError("Live account filter response must include all categories")
        categorized = {"exchangeFilters": [], "symbolFilters": [], "assetFilters": []}
    elif isinstance(raw, dict):
        unknown_categories = set(raw) - {"exchangeFilters", "symbolFilters", "assetFilters", "rateLimits"}
        if unknown_categories or not all(isinstance(raw.get(k), list) for k in ("exchangeFilters", "symbolFilters", "assetFilters")):
            raise ValueError("Unknown or missing account filter categories")
        categorized = raw
    else:
        raise ValueError("Malformed account-specific filters")
    if sized.get("status") != "READY":
        return {"status": "NO_ORDER", "reason": "no_executable_quantity"}
    quantity = number(sized["quantity"])
    reference = number(market["reference_price"])
    limit_price = number(sized["limit_price"])
    effective_price = max(reference, limit_price)
    symbol_filters = list(market["filters"])
    for item in categorized["symbolFilters"]:
        if "filters" in item:
            if item.get("symbol") == "BTCUSDT":
                symbol_filters.extend(item["filters"])
        elif item.get("symbol", "BTCUSDT") == "BTCUSDT":
            symbol_filters.append(item)
    ignored_symbol = {"MARKET_LOT_SIZE", "ICEBERG_PARTS", "MAX_NUM_ALGO_ORDERS",
                      "MAX_NUM_ICEBERG_ORDERS", "TRAILING_DELTA", "MAX_NUM_ORDER_AMENDS", "MAX_NUM_ORDER_LISTS"}
    for row in symbol_filters:
        kind = row.get("filterType")
        if kind == "PRICE_FILTER":
            low, high, tick = (number(row[k]) for k in ("minPrice", "maxPrice", "tickSize"))
            if (low > 0 and limit_price < low or high > 0 and limit_price > high
                    or tick > 0 and limit_price % tick != 0):
                raise ValueError("LIMIT IOC price violates PRICE_FILTER")
        elif kind == "LOT_SIZE":
            low, high, step = (number(row[k]) for k in ("minQty", "maxQty", "stepSize"))
            if (low > 0 and quantity < low or high > 0 and quantity > high
                    or step > 0 and quantity % step != 0):
                raise ValueError("LIMIT IOC quantity violates LOT_SIZE")
        elif kind in {"MIN_NOTIONAL", "NOTIONAL"}:
            minimum = number(row.get("minNotional", row.get("notional", "0")))
            maximum = number(row.get("maxNotional", "0"))
            notional = quantity * limit_price
            if notional < minimum or maximum > 0 and notional > maximum:
                raise ValueError("LIMIT IOC notional violates a notional filter")
        elif kind in {"PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"}:
            prefix = ("bid" if sized["side"] == "BUY" else "ask") if kind == "PERCENT_PRICE_BY_SIDE" else ""
            lower_key = prefix + "MultiplierDown" if prefix else "multiplierDown"
            upper_key = prefix + "MultiplierUp" if prefix else "multiplierUp"
            if not reference*number(row[lower_key]) <= limit_price <= reference*number(row[upper_key]):
                raise ValueError("LIMIT IOC price violates percentage price bands")
        elif kind == "MAX_POSITION":
            if sized["side"] == "BUY" and balances["BTC"]["total"] + quantity > number(row["maxPosition"]):
                raise ValueError("MAX_POSITION would include and exceed the reserved BTC balance")
        elif kind == "MAX_NUM_ORDERS":
            if number(row["maxNumOrders"]) < 1:
                raise ValueError("MAX_NUM_ORDERS disallows a new order")
        elif kind not in ignored_symbol:
            raise ValueError(f"Unsupported account/symbol filter: {kind}")
    for row in categorized["exchangeFilters"]:
        kind = row.get("filterType")
        if kind == "EXCHANGE_MAX_NUM_ORDERS":
            if number(row["maxNumOrders"]) < 1:
                raise ValueError("EXCHANGE_MAX_NUM_ORDERS disallows an order")
        elif kind not in {"EXCHANGE_MAX_NUM_ALGO_ORDERS", "EXCHANGE_MAX_NUM_ICEBERG_ORDERS", "EXCHANGE_MAX_NUM_ORDER_LISTS"}:
            raise ValueError(f"Unsupported account exchange filter: {kind}")
    for row in categorized["assetFilters"]:
        if row.get("filterType") != "MAX_ASSET":
            raise ValueError(f"Unsupported account asset filter: {row.get('filterType')}")
        asset, limit = row.get("asset"), number(row["limit"])
        if limit <= 0:
            raise ValueError("Invalid MAX_ASSET limit")
        amount = quantity if asset == "BTC" else quantity * effective_price if asset == "USDT" else Decimal(0)
        if amount > limit:
            raise ValueError(f"MAX_ASSET order limit exceeded for {asset}")
    return {"status": "VALID", "reason": "known_account_and_limit_filters_checked"}


class Engine:
    def __init__(self, gateway, store: Store, fee_rate=Decimal(".001"), *, clock_ms=None, stop_requested=None):
        self.gateway, self.store = gateway, store
        self.fee_rate = number(fee_rate)
        if not 0 <= self.fee_rate < 1:
            raise ValueError("Invalid taker fee")
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.stop_requested = stop_requested or (lambda: False)
        self._mutex = asyncio.Lock()

    def status(self):
        blocker = self.store.blocker()
        pending = self.store.pending()
        return {"status": "BLOCKED" if blocker else "PENDING" if pending else "READY",
                "reason": blocker["reason"] if blocker else "pending_intent" if pending else "local_state_ready",
                "mode": self.store.binding["mode"], "symbol": self.store.binding["symbol"],
                "wallet": self.store.wallet(), "pending": pending, "blocker": blocker,
                "fees": self.store.fees(), "fill_count": self.store.fill_count(),
                "latest_decision": self.store.latest_decision()}

    def _block(self, reason, *, sticky=False, **details):
        self.store.set_blocker(reason, sticky=sticky, **details)
        return self.status()

    @staticmethod
    def _safe_error(exc):
        # Transport exceptions can contain signed URLs. Never persist str(exc).
        return {"error_type": type(exc).__name__,
                "error_code": exc.code if isinstance(getattr(exc, "code", None), int) else None,
                "http_status": exc.status if isinstance(getattr(exc, "status", None), int) else None}

    @staticmethod
    def _balances(account):
        balances = {}
        for row in account.get("balances", []):
            asset = row.get("asset")
            if asset in balances:
                raise ValueError("Duplicate account balance asset")
            free, locked = number(row["free"]), number(row["locked"])
            if free < 0 or locked < 0:
                raise ValueError("Negative account asset balance")
            balances[asset] = {"free": free, "locked": locked, "total": free + locked}
        for asset in ("BTC", "USDT"):
            balances.setdefault(asset, {"free": Decimal(0), "locked": Decimal(0), "total": Decimal(0)})
        return balances

    async def _account_check(self, *, initialize=False):
        account = await self.gateway.account()
        if account.get("canTrade") is not True:
            return self._block("account_cannot_trade")
        balances = self._balances(account)
        open_orders = await self.gateway.open_orders(all_symbols=True)
        if not isinstance(open_orders, list):
            raise ValueError("Open order response must be a list")
        if open_orders:
            return self._block("foreign_or_unresolved_open_orders", count=len(open_orders))
        if any(balances[a]["locked"] > 0 for a in ("BTC", "USDT")):
            return self._block("locked_btc_or_usdt_balance")
        wallet = self.store.wallet()
        if wallet is None:
            if not initialize:
                return {"status": "READY", "reason": "allocation_not_initialized", "balances": balances}
            self.store.initialize_wallet(total_btc=balances["BTC"]["total"], total_quote=balances["USDT"]["total"],
                                         account_uid=account.get("uid"))
            wallet = self.store.wallet()
        elif wallet["account_uid"] is not None and str(account.get("uid")) != wallet["account_uid"]:
            return self._block("account_uid_changed", sticky=True)
        else:
            self.store.bind_uid(account.get("uid"))
        expected_btc = number(wallet["btc"]) + number(wallet["reserve_btc"])
        expected_quote = number(wallet["quote"]) + number(wallet["reserve_quote"])
        if (abs(balances["BTC"]["total"] - expected_btc) > BTC_TOL
                or abs(balances["USDT"]["total"] - expected_quote) > QUOTE_TOL):
            return self._block("account_balance_drift", expected_btc=text_number(expected_btc),
                actual_btc=text_number(balances["BTC"]["total"]), expected_usdt=text_number(expected_quote),
                actual_usdt=text_number(balances["USDT"]["total"]))
        return {"status": "READY", "reason": "account_reconciled", "balances": balances}

    def _market_check(self, decision_id, market):
        if (market.get("symbol") != "BTCUSDT" or market.get("base_asset") != "BTC"
                or market.get("quote_asset") != "USDT" or market.get("status") != "TRADING"
                or market.get("spot_allowed") is not True):
            raise ValueError("BTCUSDT spot market is unavailable or mismatched")
        received = number(market["received_at_ms"])
        age = self.clock_ms() - received
        if age < -5000 or age > 30000:
            raise ValueError("Public market snapshot is stale or from the future")
        server_ms = number(market["server_time_ms"])
        if abs(server_ms - received) > 60000:
            raise ValueError("Server clock and receipt time differ excessively")
        day = datetime.fromtimestamp(float(server_ms) / 1000, timezone.utc).date().isoformat()
        if decision_id != day:
            raise ValueError("Only the current UTC daily decision can execute; no historical replay")
        bid, ask, reference = (number(market[key]) for key in ("bid", "ask", "reference_price"))
        if min(bid, ask, reference) <= 0 or bid > ask:
            raise ValueError("Invalid spot bid/ask/reference prices")

    def _extra_filters(self, market, sized, balances):
        return validate_plan_filters(market, sized, balances, self.store.binding["mode"])

    def _normalize_trades(self, decision, order, trades):
        if not isinstance(trades, list):
            raise ValueError("Trades must be a list")
        output, seen = [], set()
        for trade in trades:
            if (trade.get("symbol") != "BTCUSDT" or str(trade.get("orderId")) != str(order["orderId"])
                    or trade.get("isBuyer") is not (decision["side"] == "BUY")):
                raise ValueError("Trade symbol, order or side differs from the committed intent")
            trade_id = str(int(trade["id"]))
            if int(trade_id) < 0 or trade_id in seen:
                raise ValueError("Invalid or duplicate trade ID in gateway response")
            seen.add(trade_id)
            quantity, price, quote, fee = (number(trade[k]) for k in ("qty", "price", "quoteQty", "commission"))
            if min(quantity, price, quote) <= 0 or fee < 0 or abs(quote - quantity*price) > QUOTE_TOL:
                raise ValueError("Invalid trade quantities, quote value or commission")
            asset = trade.get("commissionAsset")
            if not isinstance(asset, str) or not asset:
                raise ValueError("Trade commission asset is missing")
            output.append({"trade_id": trade_id, "order_id": str(order["orderId"]), "side": decision["side"],
                           "quantity": text_number(quantity), "quote_quantity": text_number(quote),
                           "price": text_number(price), "fee": text_number(fee), "fee_asset": asset})
        return sorted(output, key=lambda row: int(row["trade_id"]))

    async def _reconcile_intent(self, decision):
        order = await self.gateway.get_order(decision["client_id"])
        if order is None:
            if decision["order_status"] in {"REJECTED_BEFORE_ACCEPTANCE", "STOPPED_BEFORE_SUBMISSION"}:
                stopped = decision["order_status"] == "STOPPED_BEFORE_SUBMISSION"
                reason = "stop_requested_before_submission" if stopped else "definitive_exchange_rejection_no_same_day_retry"
                self.store.update_order(decision["decision_id"],
                                        order_status="CANCELED_BEFORE_SUBMIT" if stopped else "REJECTED",
                                        complete=True, reason=reason)
                self.store.clear_transient_blocker()
                return {"status": "STOPPED" if stopped else "REJECTED", "reason": reason,
                        "decision_id": decision["decision_id"]}
            return self._block("order_outcome_unknown_no_resubmit", decision_id=decision["decision_id"], client_id=decision["client_id"])
        if (order.get("symbol") != "BTCUSDT" or order.get("clientOrderId") != decision["client_id"]
                or order.get("side") != decision["side"] or order.get("type") != "LIMIT"
                or order.get("timeInForce") != "IOC"
                or number(order.get("price")) != number(decision["limit_price"])):
            return self._block("exchange_order_differs_from_intent", sticky=True)
        status = order.get("status")
        if status not in ORDER_STATES:
            return self._block("unsupported_exchange_order_status", order_status=str(status))
        requested, executed, quote = (number(order[k]) for k in ("origQty", "executedQty", "cummulativeQuoteQty"))
        if (abs(requested-number(decision["quantity"])) > Decimal("1e-12") or executed < 0
                or quote < 0 or executed > requested+Decimal("1e-12")):
            return self._block("exchange_order_quantities_differ_from_intent", sticky=True)
        self.store.update_order(decision["decision_id"], order_id=order["orderId"], order_status=status)
        trades = self._normalize_trades(decision, order, await self.gateway.trades(order["orderId"]))
        self.store.apply_fills(decision["decision_id"], trades)
        unsupported = self.store.unsupported_fee_assets()
        if unsupported:
            return self._block("unsupported_fee_asset_cost_recorded", sticky=True, assets=unsupported)
        wallet = self.store.wallet()
        if number(wallet["btc"]) < -Decimal("1e-12") or number(wallet["quote"]) < -QUOTE_TOL:
            return self._block("allocation_budget_consumed_reserve", sticky=True)
        known_qty = sum((number(row["quantity"]) for row in trades), Decimal(0))
        known_quote = sum((number(row["quote_quantity"]) for row in trades), Decimal(0))
        if abs(known_qty-executed) > Decimal("1e-12") or abs(known_quote-quote) > QUOTE_TOL:
            return self._block("trade_history_not_yet_complete", decision_id=decision["decision_id"])
        if status not in TERMINAL:
            return self._block("order_still_open", decision_id=decision["decision_id"])
        reconciled = await self._account_check()
        if reconciled["status"] != "READY":
            return reconciled
        self.store.update_order(decision["decision_id"], complete=True, reason="actual_fills_and_account_reconciled")
        self.store.clear_transient_blocker()
        return {"status": "COMPLETE", "reason": "actual_fills_and_account_reconciled", "decision_id": decision["decision_id"],
                "order_status": status, "wallet": self.store.wallet(), "fill_count": self.store.fill_count()}

    async def _recover(self):
        sticky = self.store.blocker()
        if sticky and sticky.get("sticky") and sticky["reason"] not in {
                "unsupported_fee_asset_cost_recorded", "allocation_budget_consumed_reserve"}:
            return self.status()
        for decision in self.store.pending():
            outcome = await self._reconcile_intent(decision)
            if outcome["status"] not in {"COMPLETE", "REJECTED", "STOPPED"}:
                return outcome
        if self.store.wallet() is not None:
            outcome = await self._account_check()
            if outcome["status"] != "READY":
                return outcome
        self.store.clear_transient_blocker()
        return self.status()

    async def recover(self):
        async with self._mutex:
            try:
                return await self._recover()
            except Exception as exc:
                return self._block("recovery_read_or_validation_failed", **self._safe_error(exc))

    async def execute(self, decision_id: str, target_btc_fraction: Decimal, market: dict):
        async with self._mutex:
            try:
                target = number(target_btc_fraction)
                if not 0 <= target <= 1:
                    raise ValueError("Target BTC fraction must be between zero and one")
                self._market_check(decision_id, market)
                outcome = await self._recover()
                if outcome["status"] != "READY":
                    return outcome
                prior = self.store.decision(decision_id)
                if prior:
                    if number(prior["target"]) != target:
                        return self._block("same_day_target_changed", decision_id=decision_id)
                    return {"status": "ALREADY_DECIDED", "reason": prior["reason"], "decision": prior}
                reconciled = await self._account_check(initialize=True)
                if reconciled["status"] != "READY":
                    return reconciled
                wallet = self.store.wallet()
                sizing = plan_order(btc_balance=wallet["btc"], quote_balance=wallet["quote"],
                                    target_btc_fraction=target, market=market, fee_rate=self.fee_rate)
                limit_price = number(sizing["limit_price"])
                snapshot = {key: market[key] for key in ("server_time_ms", "received_at_ms", "bid", "ask", "reference_price")}
                snapshot = {key: str(value) for key, value in snapshot.items()}
                snapshot["filter_sha256"] = hashlib.sha256(encoded(market["filters"]).encode()).hexdigest()
                if self.stop_requested():
                    return {"status": "STOPPED", "reason": "stop_requested_before_intent"}
                if sizing["status"] != "READY":
                    decision = self.store.create_decision(decision_id, target, phase="NOOP", reason=sizing["reason"], market=snapshot)
                    return {"status": "NOOP", "reason": sizing["reason"], "decision": decision}
                self._extra_filters(market, sizing, reconciled["balances"])
                # A private read can be slow. Revalidate the public snapshot
                # immediately before persisting and submitting a new intent.
                self._market_check(decision_id, market)
                decision = self.store.create_decision(decision_id, target, side=sizing["side"],
                    quantity=sizing["quantity"], limit_price=limit_price, market=snapshot)
                if self.stop_requested():
                    self.store.update_order(decision_id, order_status="CANCELED_BEFORE_SUBMIT", complete=True,
                                            reason="stop_requested_before_submission")
                    return {"status": "STOPPED", "reason": "stop_requested_before_submission", "decision_id": decision_id}
                try:
                    await self.gateway.place_order(decision["client_id"], sizing["side"], number(sizing["quantity"]), limit_price=limit_price)
                except Exception as exc:
                    # Whether before or after acceptance, never issue another
                    # POST for this durable intent. Reads can resolve it later.
                    stopped_before_post = getattr(exc, "definitely_not_submitted", False) is True
                    definitive_rejection = (stopped_before_post or
                        (getattr(exc, "maybe_sent", None) is False and isinstance(getattr(exc, "code", None), int)))
                    # Persist this evidence before any follow-up network read:
                    # a failed query or restart must not turn a known rejection
                    # into an permanently ambiguous intent.
                    self.store.update_order(decision_id, reason="submission_exception_query_only",
                        order_status="STOPPED_BEFORE_SUBMISSION" if stopped_before_post else
                        "REJECTED_BEFORE_ACCEPTANCE" if definitive_rejection else None)
                    self.store.set_blocker("submission_outcome_requires_reconciliation", **self._safe_error(exc))
                return await self._reconcile_intent(self.store.decision(decision_id))
            except Exception as exc:
                return self._block("execution_read_or_validation_failed", **self._safe_error(exc))
