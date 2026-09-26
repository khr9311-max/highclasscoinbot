"""One owner for BTC budgets, persisted IOC intents and exchange-held futures stops."""
import asyncio
import hashlib
import json
import time
from decimal import Decimal

from btc_spot.store import number
from .filters import account_balances, validate_plan_filters, plan_order
from .signals import alt_signal, coinm_signal

D = Decimal
TERMINAL = {"FILLED", "EXPIRED", "CANCELED", "REJECTED", "EXPIRED_IN_MATCH"}


def allocation_snapshot(config, spot_equity, coinm_equity):
    """Report drift against the initial venue weights without moving funds."""
    spot, coin = number(spot_equity), number(coinm_equity)
    total = spot + coin
    if total <= 0:
        raise ValueError("Portfolio BTC equity must be positive")
    target_spot = (number(config.spot_fraction) if config.strategy_mode == "aggressive"
                   else number(config.spot_btc) / config.total)
    spot_excess = spot - total * target_spot
    threshold = total*D("0.05") if config.strategy_mode == "aggressive" else max(total * D("0.05"), D("0.0001"))
    return {"spot_equity_btc": str(spot), "coinm_equity_btc": str(coin),
            "spot_weight_pct": str(spot / total * 100),
            "coinm_weight_pct": str(coin / total * 100),
            "target_spot_weight_pct": str(target_spot * 100),
            "target_coinm_weight_pct": str((1-target_spot) * 100),
            "spot_excess_btc": str(spot_excess),
            "rebalance_review": abs(spot_excess) >= threshold}


def fresh(market):
    age = time.time_ns()//1_000_000 - int(market["received_at_ms"])
    if not -5000 <= age <= 30000:
        raise ValueError("Market data is stale")


def coin_plan(config, market, signal, fee, available, equity):
    spec = market["spec"]
    direction = signal["direction"]
    if direction not in (-1, 1):
        return {"status": "SKIP", "reason": "neutral_signal"}
    entry = spec.round_price(number(market["ask" if direction > 0 else "bid"])*
                            (D("1.0003") if direction > 0 else D(".9997")), "up" if direction > 0 else "down")
    stop = spec.round_price_away(number(market["mark"])*(1-direction*number(signal["stop_fraction"])), direction, True)
    if stop <= 0 or direction*(entry-stop) <= 0:
        raise ValueError("Invalid inverse stop geometry")
    # Inverse loss per contract: face * |1/entry - 1/exit|, plus fees and stop slippage.
    exit_price = stop*(D(".9995") if direction > 0 else D("1.0005"))
    loss = spec.contract_size*abs(1/entry-1/exit_price) + spec.contract_size*fee*(1/entry+1/exit_price)
    risk = min(config.total, equity)*number(config.risk_fraction)
    max_qty = min(risk/loss, min(number(config.coinm_btc), available)*entry/spec.contract_size*number(config.coinm_max_exposure),
                  available/(spec.contract_size/entry*(1+fee)), spec.max_qty)
    quantity = spec.round_qty_down(max_qty, market=False)
    valid, _ = spec.check_qty(quantity, market=False)
    if not valid:
        return {"status": "SKIP", "reason": "minimum_contract_exceeds_risk_or_budget",
                "one_contract_loss_btc": str(loss), "risk_budget_btc": str(risk)}
    if not spec.check_price(entry)[0] or not spec.check_price(stop)[0]:
        raise ValueError("COIN-M price filter failed")
    mark = number(market["mark"])
    if (spec.percent_down is not None and entry < mark*spec.percent_down
            or spec.percent_up is not None and entry > mark*spec.percent_up):
        raise ValueError("COIN-M price band failed")
    return {"status": "READY", "symbol": "BTCUSD_PERP", "side": "BUY" if direction > 0 else "SELL",
            "quantity": str(quantity), "price": str(entry), "stop": str(stop), "reduce_only": False,
            "risk_btc": str(quantity*loss)}


class Engine:
    def __init__(self, venues, store, config):
        self.v, self.s, self.c = venues, store, config

    def readiness(self, account):
        if not self.c.coinm_managed:
            return self.spot_only_readiness(account)
        reasons = []
        balances = account_balances(account["spot"])
        position, coin = account["position"], account["coin"]
        perms = account["permissions"]
        if account["spot"].get("canTrade") is not True or not coin.can_trade:
            reasons.append("account_trading_disabled")
        if perms.get("enableSpotAndMarginTrading") is not True or perms.get("enableFutures") is not True:
            reasons.append("spot_and_futures_permissions_required")
        if self.c.strategy_mode == "aggressive" and account.get("spot_bnb_burn") is not False:
            reasons.append("disable_spot_bnb_fee_payment_before_aggressive_trading")
        if (self.c.strategy_mode == "aggressive" and self.c.rebalance_mode == "auto"
                and perms.get("permitsUniversalTransfer") is not True):
            reasons.append("auto_rebalance_requires_universal_transfer_permission")
        if account["hedge_mode"] or position.position_side != "BOTH" or position.margin_type != "isolated":
            reasons.append("coinm_requires_existing_isolated_one_way_configuration")
        if not 1 <= position.leverage <= self.c.max_leverage:
            reasons.append("coinm_leverage_above_configured_limit")
        if self.c.strategy_mode == "aggressive" and number(self.c.spot_fraction) < 1 and position.leverage != 3:
            reasons.append("aggressive_coinm_requires_exchange_leverage_3")
        if any(p.position_amt for p in coin.positions if p.symbol != "BTCUSD_PERP"):
            reasons.append("other_futures_positions_present")
        if account["spot_orders"] or account["coin_orders"]:
            reasons.append("open_orders_present")
        if number(coin.asset("BTC").open_order_initial_margin) > 0:
            reasons.append("btc_futures_margin_reserved_by_open_order")
        owned_stop = (self.s.get("stop") or {}).get("id") if self.s else None
        if any(a.client_id != owned_stop for a in account["algos"]):
            reasons.append("foreign_conditional_orders_present")
        if self.c.strategy_mode == "intraday" and self.s and self.s.get("wallet") is not None:
            entry = self.s.get("alt_entry") or {}
            if any(number(v) > 0 for a, v in self.s.get("wallet").items() if a != "BTC") and not entry.get("entered_at_ms"):
                reasons.append("intraday_spot_entry_metadata_migration_required")
            if position.position_amt and not (self.s.get("position_entry") or {}).get("entered_at_ms"):
                reasons.append("intraday_coinm_entry_metadata_migration_required")
        if not self.s or self.s.get("wallet") is None:
            if position.position_amt:
                reasons.append("unowned_coinm_position")
            if balances.get("BTC", {}).get("free", D(0)) < number(self.c.spot_btc):
                reasons.append("spot_btc_allocation_not_funded")
            btc = coin.asset("BTC")
            if number(btc.available_balance) < number(self.c.coinm_btc):
                reasons.append("coinm_btc_allocation_not_funded_transfer_required")
            if abs(number(btc.wallet_balance)-number(self.c.coinm_btc)) > D(".00000001"):
                reasons.append("coinm_wallet_must_match_dedicated_budget")
        return reasons

    def spot_only_readiness(self, account):
        """COIN-M belongs to the user: only spot is checked, and the ledger must own nothing there."""
        reasons = []
        if account["spot"].get("canTrade") is not True:
            reasons.append("account_trading_disabled")
        if account["permissions"].get("enableSpotAndMarginTrading") is not True:
            reasons.append("spot_permission_required")
        if account.get("spot_bnb_burn") is not False:
            reasons.append("disable_spot_bnb_fee_payment_before_aggressive_trading")
        if account["spot_orders"]:
            reasons.append("open_orders_present")
        if not self.s or self.s.get("wallet") is None:
            reasons.append("registered_ledger_required_for_spot_only")
        elif number(self.s.get("coin_qty", "0")) or self.s.get("stop") or self.s.get("position_stop"):
            reasons.append("ledger_still_owns_coinm_position")
        return reasons

    def coinm_intent_pending(self):
        return any(p["venue"] == "coinm" for p in self.s.pending())

    async def initialize(self, account):
        if self.s.get("wallet") is not None:
            return
        reasons = self.readiness(account)
        if reasons:
            raise ValueError(";".join(reasons))
        uid = account["spot"].get("uid")
        if not uid:
            raise ValueError("Missing spot account identity")
        balances = account_balances(account["spot"])
        assets = {"BTC", *(s[:-3] for s in self.c.symbols)}
        reserved = {a: str(balances.get(a, {}).get("total", D(0)) - (number(self.c.spot_btc) if a == "BTC" else 0)) for a in assets}
        with self.s.transaction():
            self.s.put("wallet", {a: self.c.spot_btc if a == "BTC" else "0" for a in assets})
            self.s.put("reserve", reserved)
            self.s.put("uid", str(uid))
            self.s.put("coin_qty", "0")
            self.s.put("coin_wallet_initial", self.c.coinm_btc)
            self.s.event("initialized", {"spot_btc": self.c.spot_btc, "coinm_btc": self.c.coinm_btc})

    def check_spot(self, account):
        if str(account["spot"].get("uid")) != self.s.get("uid"):
            raise ValueError("Account identity changed")
        actual = account_balances(account["spot"])
        for asset, value in self.s.get("wallet").items():
            row = actual.get(asset, {"total": D(0), "locked": D(0)})
            if row["locked"] or abs(row["total"]-number(value)-number(self.s.get("reserve")[asset])) > D(".00000001"):
                raise ValueError("Spot balance drift or locked funds")

    async def reconcile(self, intent):
        req = json.loads(intent["request"])
        ident, venue = intent["id"], intent["venue"]
        if venue == "spot":
            gateway = self.v.spots[req["symbol"]]
            order = await gateway.get_order(ident)
            if order is None:
                return False
            if (order.get("clientOrderId") != ident or order.get("symbol") != req["symbol"]
                    or order.get("side") != req["side"] or order.get("type") != "LIMIT"
                    or order.get("timeInForce") != "IOC" or number(order["price"]) != number(req["price"])
                    or number(order["origQty"]) != number(req["quantity"])):
                raise ValueError("Spot order differs from intent")
            rows = await gateway.trades(int(order["orderId"]))
            executed, cumulative = number(order["executedQty"]), number(order["cummulativeQuoteQty"])
            if executed < 0 or executed > number(req["quantity"]) or cumulative < 0:
                raise ValueError("Invalid executed spot quantities")
            qty, quote, seen = D(0), D(0), set()
            wallet = {a: number(v) for a, v in self.s.get("wallet").items()}
            with self.s.transaction():
                for row in rows:
                    q, p, cash, fee = (number(row[k]) for k in ("qty", "price", "quoteQty", "commission"))
                    if (row["symbol"] != req["symbol"] or str(row["orderId"]) != str(order["orderId"])
                            or row["isBuyer"] is not (req["side"] == "BUY") or row["id"] in seen
                            or min(q, p, cash) <= 0 or fee < 0 or abs(q*p-cash) > D(".00000001")):
                        raise ValueError("Invalid spot fill")
                    seen.add(row["id"])
                    qty += q
                    quote += cash
                    if self.s.fill("spot", req["symbol"], row["id"], row):
                        direction = 1 if req["side"] == "BUY" else -1
                        wallet[req["symbol"][:-3]] += direction*q
                        wallet["BTC"] -= direction*cash
                        if row["commissionAsset"] not in (req["symbol"][:-3], "BTC"):
                            self.s.put("halt", "external_fee_asset_requires_reconciliation")
                        else:
                            wallet[row["commissionAsset"]] -= fee
                if min(wallet.values()) < -D(".000000000001"):
                    raise ValueError("Fill consumed reserved assets")
                self.s.put("wallet", wallet)
                done = order["status"] in TERMINAL and qty == executed and abs(quote-cumulative) <= D(".00000001")
                if done:
                    self.s.complete(ident, order)
                    if req["side"] == "BUY" and executed:
                        self.s.put("alt_entry", {"symbol": req["symbol"], "price": str(cumulative/executed),
                            "stop_fraction": req.get("stop_fraction", self.c.alt_stop_fraction),
                            "entered_at_ms": intent["created_ms"]})
                    elif req["side"] == "SELL" and executed and self.c.strategy_mode == "intraday":
                        self.s.put("spot_cooldown_until", time.time_ns()//1_000_000+self.c.intraday_cooldown_seconds*1000)
            return done
        order = await self.v.coin.get_order("BTCUSD_PERP", ident)
        if order.status == "NOT_FOUND":
            return False
        if (order.client_id != ident or order.symbol != "BTCUSD_PERP" or order.side != req["side"]
                or order.orig_qty != number(req["quantity"]) or order.order_type != ("MARKET" if req.get("emergency") else "LIMIT")
                or order.reduce_only != req.get("reduce_only", False)
                or not req.get("emergency") and number(order.raw.get("price")) != number(req["price"])):
            raise ValueError("COIN-M order differs from intent")
        rows = await self.v.coin.get_user_trades("BTCUSD_PERP", start_ms=intent["created_ms"]-60000, order_id=order.exchange_id)
        qty, seen = D(0), set()
        with self.s.transaction():
            for fill in rows:
                if (fill.symbol != "BTCUSD_PERP" or fill.order_id != str(order.exchange_id)
                        or fill.side != req["side"] or fill.trade_id in seen or fill.qty <= 0
                        or number(fill.price) <= 0 or number(fill.commission) < 0 or fill.commission_asset != "BTC"):
                    raise ValueError("Invalid COIN-M fill")
                seen.add(fill.trade_id)
                qty += fill.qty
                if self.s.fill("coinm", fill.symbol, fill.trade_id, fill.__dict__):
                    self.s.put("coin_qty", number(self.s.get("coin_qty")) + (1 if fill.side == "BUY" else -1)*fill.qty)
            done = order.status in TERMINAL and qty == order.executed_qty
            if done:
                self.s.complete(ident, order.raw)
                if not req.get("reduce_only") and qty:
                    if not req.get("aggressive_resize"):
                        price = qty/sum((f.qty/number(f.price) for f in rows), D(0))
                        stop_price = req["stop"]
                        if (self.c.strategy_mode == "aggressive" and self.s.get("stop") is None
                                and getattr(self.v, "spec", None) is not None):
                            direction = 1 if req["side"] == "BUY" else -1
                            stop_price = str(self.v.spec.round_price_away(
                                price*(1-direction*number(self.c.stop_fraction)), direction, True))
                        self.s.put("position_stop", stop_price)
                        self.s.put("position_entry", {"price": str(price), "stop_fraction": str(abs(1-number(stop_price)/price)),
                                   "entered_at_ms": intent["created_ms"]})
                elif req.get("reduce_only") and qty and self.c.strategy_mode == "intraday":
                    self.s.put("coin_cooldown_until", time.time_ns()//1_000_000+self.c.intraday_cooldown_seconds*1000)
        return done

    async def submit(self, venue, key, req):
        fresh(req.pop("_market"))
        ident, new = self.s.intent(venue, key, req)
        if new:
            try:
                await self.v.submit(ident, venue, req)
            except Exception as exc:
                # Every uncertainty, including an interrupted acknowledgement, remains query-only.
                self.s.event("submission_error", {"id": ident, "error_type": type(exc).__name__})
        intent = next((p for p in self.s.pending() if p["id"] == ident), None)
        if intent:
            await self.reconcile(intent)

    async def protect(self, account):
        try:
            await self._protect(account)
        except Exception:
            position = account["position"]
            expected = number(self.s.get("coin_qty", "0"))
            if position.position_amt and position.position_amt == expected:
                self.s.put("halt", "protection_failed_emergency_close_requested")
                stop = self.s.get("stop") or {"id": "missing"}
                req = {"symbol": "BTCUSD_PERP", "side": "SELL" if expected > 0 else "BUY",
                       "quantity": str(abs(expected)), "reduce_only": True, "emergency": True}
                ident, new = self.s.intent("coinm", "emergency:"+stop["id"], req)
                if new:
                    try:
                        await self.v.submit(ident, "coinm", req)
                    except Exception as exc:
                        self.s.event("emergency_close_error", {"error_type": type(exc).__name__})
                self.s.event("protection_failure", {"close_id": ident, "position": str(expected)})
            raise

    async def _protect(self, account):
        position = account["position"]
        stop = self.s.get("stop")
        if not position.position_amt:
            if stop:
                observed = await self.v.coin.get_algo_order("BTCUSD_PERP", stop["id"])
                if observed.status in {"NEW", "TRIGGERING"}:
                    await self.v.coin.cancel_order("BTCUSD_PERP", stop["id"], True)
                    observed = await self.v.coin.get_algo_order("BTCUSD_PERP", stop["id"])
                if observed.status in {"NEW", "TRIGGERING"}:
                    raise ValueError("Protective stop cancellation not confirmed")
                if number(self.s.get("coin_qty", "0")):
                    if not observed.actual_order_id:
                        raise ValueError("Position disappeared without a confirmed protective fill")
                    fills = await self.v.coin.get_user_trades("BTCUSD_PERP", start_ms=stop["created_ms"]-60000,
                                                             order_id=observed.actual_order_id)
                    total = sum((f.qty for f in fills), D(0))
                    if total != abs(number(self.s.get("coin_qty"))):
                        raise ValueError("Protective stop fills incomplete")
                    with self.s.transaction():
                        for f in fills:
                            if f.side != stop["side"] or f.order_id != str(observed.actual_order_id):
                                raise ValueError("Protective fill identity mismatch")
                            self.s.fill("coinm", f.symbol, f.trade_id, f.__dict__)
                        self.s.put("coin_qty", "0")
                        self.s.event("stop_filled", {"id": stop["id"]})
                        if self.c.strategy_mode == "aggressive":
                            self.s.put("aggressive_blocked_side", 1 if stop["side"] == "SELL" else -1)
                        if self.c.strategy_mode == "intraday":
                            self.s.put("coin_cooldown_until", time.time_ns()//1_000_000+self.c.intraday_cooldown_seconds*1000)
                self.s.put("stop", None)
            elif number(self.s.get("coin_qty", "0")):
                raise ValueError("COIN-M position disappeared without owned stop or close fills")
            self.s.put("position_stop", None)
            self.s.put("position_entry", None)
            return
        expected = number(self.s.get("coin_qty", "0"))
        pending_entry = next((json.loads(p["request"]) for p in self.s.pending()
                              if p["venue"] == "coinm" and not json.loads(p["request"]).get("reduce_only")), None)
        if position.position_amt != expected and not pending_entry:
            raise ValueError("Unowned or externally modified COIN-M position")
        price = (pending_entry or {}).get("stop") or self.s.get("position_stop")
        if not price:
            raise ValueError("Missing committed position stop")
        side = "SELL" if position.position_amt > 0 else "BUY"
        if stop:
            observed = await self.v.coin.get_algo_order("BTCUSD_PERP", stop["id"])
            if (observed.status == "NEW" and observed.side == side and observed.close_position
                    and observed.working_type == "MARK_PRICE" and number(observed.trigger_price) == number(price)):
                return
            if observed.status in {"TRIGGERING", "TRIGGERED"}:
                raise ValueError("Protective stop executing; wait for reconciliation")
            self.s.put("halt", "protective_stop_missing_or_rejected")
            raise ValueError("Protective stop missing or rejected; no resubmission")
        ident = "bsg_"+hashlib.sha256((str(time.time_ns())+str(position.position_amt)).encode()).hexdigest()[:28]
        stop = {"id": ident, "price": price, "side": side, "created_ms": time.time_ns()//1_000_000}
        self.s.put("stop", stop)
        try:
            await self.v.stop(ident, side, price)
        except Exception as exc:
            self.s.event("stop_submission_error", {"error_type": type(exc).__name__})
        # Binance may accept a conditional order before it becomes visible to
        # GET /algoOrder. Never POST again: reconcile the persisted client ID.
        observed = None
        for attempt in range(5):
            try:
                observed = await self.v.coin.get_algo_order("BTCUSD_PERP", ident)
            except Exception as exc:
                self.s.event("stop_lookup_error", {"error_type": type(exc).__name__, "attempt": attempt + 1})
            else:
                if (observed.status == "NEW" and observed.client_id == ident
                        and observed.symbol == "BTCUSD_PERP" and observed.order_type == "STOP_MARKET"
                        and observed.close_position and observed.side == side
                        and observed.working_type == "MARK_PRICE"
                        and number(observed.trigger_price) == number(price)):
                    return
                if observed.status not in {"NOT_FOUND", "NEW"}:
                    break
            if attempt < 4:
                await asyncio.sleep(0.2)
        self.s.event("stop_confirmation_failed", {"id": ident,
            "status": observed.status if observed else "LOOKUP_ERROR",
            "side": observed.side if observed else None,
            "close_position": observed.close_position if observed else None,
            "working_type": observed.working_type if observed else None})
        self.s.put("halt", "protection_not_confirmed")
        raise ValueError("COIN-M protection not confirmed")

    def coinm_equity(self, account):
        return number(account["coin"].asset("BTC").margin_balance) if self.c.coinm_managed else D(0)

    def equity(self, account, markets):
        wallet = self.s.get("wallet")
        spot = number(wallet["BTC"])
        for symbol, market in markets["spot"].items():
            spot += number(wallet[symbol[:-3]])*number(market["bid"])
        return spot+self.coinm_equity(account)

    async def tick(self):
        account = await self.v.account()
        await self.initialize(account)
        if not self.c.coinm_managed and self.coinm_intent_pending():
            return {"status": "BLOCKED", "reason": "ledger_still_owns_coinm_order"}
        # Reconcile entries before testing account balances; fills may have changed them.
        for intent in self.s.pending():
            await self.reconcile(intent)
        account = await self.v.account()
        if self.c.coinm_managed:
            await self.protect(account)
        if self.s.pending():
            return {"status": "PENDING", "reason": "query_only_unknown_or_incomplete_order"}
        self.check_spot(account)
        blockers = self.readiness(account)
        if blockers:
            return {"status": "BLOCKED", "reasons": blockers}
        if self.s.get("halt"):
            return {"status": "BLOCKED", "reason": self.s.get("halt")}
        markets, fees = await self.v.markets(), await self.v.fees()
        equity = self.equity(account, markets)
        coinm_equity = self.coinm_equity(account)
        allocation = allocation_snapshot(self.c, equity-coinm_equity, coinm_equity)
        day = int(markets["coinm"]["server_time_ms"])//86_400_000
        day_start = self.s.get("day_start")
        if not day_start or day_start["day"] != day:
            day_start = {"day": day, "equity": str(equity)}
            self.s.put("day_start", day_start)
        can_enter = equity > number(day_start["equity"])*(1-number(self.c.daily_loss_fraction))
        if self.c.strategy_mode == "intraday":
            from .intraday import execute
            return await execute(self, account, markets, fees, equity, can_enter, allocation)
        if self.c.strategy_mode == "aggressive":
            from .aggressive import execute
            return await execute(self, account, markets, fees, equity)
        alt, coin = alt_signal(markets["spot"]), coinm_signal(markets["coinm"])
        result = {"status": "READY", "equity_btc": str(equity), "new_entries_allowed": can_enter,
                  "btc_usd_reference": markets["coinm"]["mark"], "alt_signal": alt, "coinm_signal": coin,
                  "allocation": allocation}
        # Exit an alt on a stop at every poll, or rotate after a completed daily signal.
        entry = self.s.get("alt_entry")
        holdings = []
        for symbol in self.c.symbols:
            held = number(self.s.get("wallet")[symbol[:-3]])
            if held > 0:
                exit_plan = plan_order(btc_balance=held, quote_balance=0, target_btc_fraction=0,
                                      market=markets["spot"][symbol], fee_rate=fees["spot"][symbol])
                if exit_plan["status"] == "READY":
                    holdings.append(symbol)
                else:
                    result.setdefault("dust", {})[symbol] = str(held)
        for symbol in holdings:
            market = markets["spot"][symbol]
            stopped = entry and entry["symbol"] == symbol and number(market["bid"]) <= number(entry["price"])*(1-number(self.c.alt_stop_fraction))
            if stopped or alt["symbol"] != symbol:
                outcome = await self.spot_trade(symbol, D(0), market, fees["spot"][symbol], account,
                    "exit:"+str(day)+":"+str(self.s.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]))
                if outcome["status"] == "SUBMITTED":
                    self.s.put("alt_day", alt["bar"])
                    return {**result, "action": outcome}
                # Do not buy another alt when an old position cannot be liquidated (dust included).
                return {**result, "action": outcome}
        pos = account["position"]
        signed = 1 if pos.position_amt > 0 else -1 if pos.position_amt < 0 else 0
        if signed and signed != coin["direction"]:
            spec, market = markets["coinm"]["spec"], markets["coinm"]
            price = spec.round_price(number(market["bid" if signed > 0 else "ask"])*(D(".9997") if signed > 0 else D("1.0003")), "down" if signed > 0 else "up")
            await self.submit("coinm", "exit:"+coin["bar"]+":"+str(self.s.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]),
                {"symbol": "BTCUSD_PERP", "side": "SELL" if signed > 0 else "BUY", "quantity": str(abs(pos.position_amt)),
                 "price": str(price), "reduce_only": True, "_market": market})
            return {**result, "action": "coinm_reduce_only_exit"}
        if can_enter and not signed and self.s.get("coin_bar") != coin["bar"]:
            plan = coin_plan(self.c, markets["coinm"], coin, fees["coinm"],
                number(account["coin"].asset("BTC").available_balance), equity)
            self.s.put("coin_bar", coin["bar"])
            if plan["status"] == "READY":
                await self.submit("coinm", "entry:"+coin["bar"], {**plan, "_market": markets["coinm"]})
                await self.protect(await self.v.account())
                return {**result, "action": "coinm_entry", "plan": plan}
            result["coinm_skip"] = plan
        if can_enter and not holdings and alt["symbol"] and self.s.get("alt_day") != alt["bar"]:
            symbol = alt["symbol"]
            wallet_btc = number(self.s.get("wallet")["BTC"])
            allocation = min(number(self.c.spot_btc)*number(self.c.alt_max_fraction),
                min(equity, self.c.total)*number(self.c.risk_fraction)/(number(self.c.alt_stop_fraction)+2*fees["spot"][symbol]+D(".001")))
            fraction = min(D(1), allocation/wallet_btc) if wallet_btc > 0 else D(0)
            self.s.put("alt_day", alt["bar"])
            result["action"] = await self.spot_trade(symbol, fraction, markets["spot"][symbol], fees["spot"][symbol], account, "entry:"+alt["bar"])
        self.s.event("evaluation", result)
        return result

    async def spot_trade(self, symbol, target, market, fee, account, key, *, stop_fraction=None):
        if (market.get("symbol") != symbol or market.get("base_asset") != symbol[:-3]
                or market.get("quote_asset") != "BTC" or market.get("status") != "TRADING"
                or market.get("spot_allowed") is not True):
            raise ValueError("BTC-quoted spot market is unavailable")
        wallet = self.s.get("wallet")
        plan = plan_order(btc_balance=wallet[symbol[:-3]], quote_balance=wallet["BTC"],
                          target_btc_fraction=target, market=market, fee_rate=fee)
        if plan["status"] != "READY":
            return plan
        market["account_filters"] = await self.v.spots[symbol].relevant_filters()
        validate_plan_filters(market, plan, account["spot"], "live")
        metadata = {"stop_fraction": str(stop_fraction)} if stop_fraction is not None else {}
        await self.submit("spot", symbol+":"+key, {"symbol": symbol, "side": plan["side"],
            "quantity": plan["quantity"], "price": plan["limit_price"], "_market": market, **metadata})
        return {"status": "SUBMITTED", "symbol": symbol, "side": plan["side"], "quantity": plan["quantity"]}
