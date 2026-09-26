"""BTC-denominated decisions for the daily rotation and COIN-M trend mode.

The signal functions are shared with swing. This module deliberately has no
credentials and can be used by read-only preparation and replay parity tests.
"""
from decimal import Decimal
from math import log, sqrt

from btc_spot.store import number
from . import timing
from .signals import closed

D = Decimal
DAY = 86_400_000
FOUR_HOURS = 14_400_000


def volatility(market):
    bars = closed(market["klines"], market["server_time_ms"], FOUR_HOURS, 200)
    closes = [float(number(row[4])) for row in bars[-181:]]
    returns = [log(b/a) for a, b in zip(closes, closes[1:])]
    mean = sum(returns)/180
    sigma = sqrt(sum((x-mean)**2 for x in returns)/179)
    if not 0 < sigma < 1:
        raise ValueError("Invalid completed four-hour volatility")
    return D(str(sigma))


def target_leverage(config, sigma):
    if config.leverage_mode == "fixed":
        return number(config.leverage_base)
    return min(number(config.leverage_max), max(number(config.leverage_min),
               number(config.leverage_base)*number(config.vol_ref)/number(sigma)))


def contract_count(leverage, equity_btc, entry, contract_size):
    if min(number(equity_btc), number(entry), number(contract_size)) <= 0:
        return 0
    return int(number(leverage)*number(equity_btc)*number(entry)/number(contract_size))


def liquidation_estimate(entry, direction, mmr, exchange_leverage=3):
    entry, mmr = number(entry), number(mmr)
    if direction not in (-1, 1) or not 0 <= mmr < 1:
        raise ValueError("Invalid liquidation inputs")
    return entry*(1+mmr)/(1+D(1)/exchange_leverage) if direction > 0 else entry*(1-mmr)/(1-D(1)/exchange_leverage)


def stop_and_liquidation(market, direction, entry, stop_fraction, *, liquidation=None):
    spec = market["spec"]
    entry = number(entry)
    stop = spec.round_price_away(entry*(1-direction*number(stop_fraction)), direction, True)
    liq = number(liquidation) if liquidation is not None else liquidation_estimate(
        entry, direction, spec.maint_margin_pct/100)
    gap = direction*(stop-liq)/entry
    return {"stop": str(stop), "liquidation": str(liq), "gap_fraction": str(gap),
            "safe": gap >= D("0.05") and spec.check_price(stop)[0]}


def rebalance_amount(spot_equity, coin_equity, spot_fraction):
    spot, coin, fraction = map(number, (spot_equity, coin_equity, spot_fraction))
    total = spot+coin
    if total <= 0 or not 0 < fraction <= 1:
        raise ValueError("Invalid sleeve allocation")
    difference = spot-fraction*total
    if abs(difference) < total*D("0.05"):
        return D(0)
    return difference  # positive: MAIN_CMFUTURE; negative: CMFUTURE_MAIN


def preview(config, markets, account, wallet=None):
    from .signals import alt_signal, coinm_signal
    alt, coin = alt_signal(markets["spot"]), coinm_signal(markets["coinm"])
    sigma = volatility(markets["coinm"])
    leverage = target_leverage(config, sigma)
    mark = number(markets["coinm"]["mark"])
    bid = number(markets["coinm"]["bid"])
    ask = number(markets["coinm"]["ask"])
    direction = coin["direction"]
    entry = (ask*D("1.0003") if direction > 0 else bid*D(".9997")) if direction else mark
    asset = account["coin"].asset("BTC")
    coin_equity = number(asset.margin_balance)
    qty = contract_count(leverage, coin_equity, entry, markets["coinm"]["spec"].contract_size)
    actual = account["position"]
    spot_equity = number(wallet["BTC"]) if wallet else D(0)
    if wallet:
        for symbol, market in markets["spot"].items():
            spot_equity += number(wallet[symbol[:-3]])*number(market["bid"])
    else:
        balances = {row["asset"]: number(row["free"])+number(row["locked"]) for row in account["spot"]["balances"]}
        # Without an owned ledger, this is a whole-account estimate and cannot be used for transfer.
        spot_equity = balances.get("BTC", D(0))
        for symbol, market in markets["spot"].items():
            spot_equity += balances.get(symbol[:-3], D(0))*number(market["bid"])
    move = rebalance_amount(spot_equity, coin_equity, config.spot_fraction) if wallet else None
    post_coin = coin_equity+move if move is not None else None
    post_contracts = (contract_count(leverage, post_coin, entry, markets["coinm"]["spec"].contract_size)
                      if post_coin is not None and number(config.spot_fraction) < 1 and post_coin > 0 else None)
    geometry = stop_and_liquidation(markets["coinm"], direction, entry, config.stop_fraction) if direction else None
    effective = abs(number(actual.position_amt))*number(markets["coinm"]["spec"].contract_size)/mark/coin_equity if coin_equity > 0 else D(0)
    return {"alt_signal": alt, "coinm_signal": coin, "sigma": str(sigma), "target_leverage": str(leverage),
            "target_contracts": qty, "current_contracts": str(actual.position_amt),
            "target_contracts_after_rebalance": post_contracts,
            "effective_leverage_after_rebalance": (str(post_contracts*number(markets["coinm"]["spec"].contract_size)/entry/post_coin)
                                                   if post_coin is not None and post_coin > 0 else None),
            "effective_leverage": str(effective), "estimated_entry": str(entry),
            "estimated_liquidation": geometry["liquidation"] if geometry else None,
            "actual_liquidation": str(actual.liquidation_price) if actual.position_amt else None,
            "disaster_stop": geometry["stop"] if geometry else None,
            "stop_gap_safe": geometry["safe"] if geometry else False,
            "spot_equity_btc": str(spot_equity), "coinm_equity_btc": str(coin_equity),
            "spot_to_coinm_btc": str(max(D(0), move)) if move is not None else None,
            "coinm_to_spot_btc": str(max(D(0), -move)) if move is not None else None,
            "allocation_source": "owned_ledger" if wallet else "whole_spot_account_estimate"}


def coin_order_plan(config, market, direction, equity, available, fee, *, current=0, stop=None, entry=None):
    """Size whole inverse contracts without exceeding the 3x hard ceiling."""
    spec = market["spec"]
    price = spec.round_price(number(market["ask" if direction > 0 else "bid"])*
                             (D("1.0003") if direction > 0 else D(".9997")), "up" if direction > 0 else "down")
    sigma = volatility(market)
    lev = target_leverage(config, sigma)
    qty = contract_count(lev, equity, price, spec.contract_size)
    qty = min(qty, contract_count(D(3), equity, price, spec.contract_size), int(spec.max_qty))
    current = int(current)
    if qty < 1 or qty <= current:
        return {"status": "SKIP", "reason": "minimum_contract_or_no_increase", "target_contracts": qty,
                "sigma": str(sigma), "leverage": str(lev)}
    add = qty-current
    fee = number(fee)
    while add and add*spec.contract_size/price*(D(1)/3+fee) > number(available):
        add -= 1
    if add == 0:
        return {"status": "SKIP", "reason": "insufficient_isolated_margin", "target_contracts": qty}
    qty = current+add
    valid, _ = spec.check_qty(D(add), market=False)
    if not valid or not spec.check_price(price)[0]:
        return {"status": "SKIP", "reason": "coinm_order_filter"}
    mark = number(market["mark"])
    if ((getattr(spec, "percent_down", None) is not None and price < mark*spec.percent_down)
            or (getattr(spec, "percent_up", None) is not None and price > mark*spec.percent_up)):
        return {"status": "SKIP", "reason": "coinm_price_band"}
    new_entry = (D(qty)/(D(current)/number(entry)+D(add)/price)) if current else price
    geometry = stop_and_liquidation(market, direction, new_entry, config.stop_fraction,
                                    liquidation=None)
    if stop is not None:
        geometry = stop_and_liquidation(market, direction, new_entry, config.stop_fraction,
                                        liquidation=geometry["liquidation"])
        geometry["stop"] = str(stop)
        geometry["gap_fraction"] = str(direction*(number(stop)-number(geometry["liquidation"]))/new_entry)
        geometry["safe"] = number(geometry["gap_fraction"]) >= D("0.05")
    if not geometry["safe"]:
        return {"status": "SKIP", "reason": "stop_too_close_to_liquidation", "geometry": geometry}
    return {"status": "READY", "symbol": "BTCUSD_PERP", "side": "BUY" if direction > 0 else "SELL",
            "quantity": str(add), "price": str(price), "stop": geometry["stop"],
            "reduce_only": False, "target_contracts": qty, "sigma": str(sigma), "leverage": str(lev),
            "estimated_liquidation": geometry["liquidation"]}


async def reduce(engine, market, signed, quantity, key):
    spec = market["spec"]
    side = "SELL" if signed > 0 else "BUY"
    price = spec.round_price(number(market["bid" if signed > 0 else "ask"])*
                             (D(".9997") if signed > 0 else D("1.0003")), "down" if signed > 0 else "up")
    await engine.submit("coinm", key, {"symbol": "BTCUSD_PERP", "side": side,
        "quantity": str(quantity), "price": str(price), "reduce_only": True, "_market": market})


async def validate_actual_position(engine, account, market):
    """A failed post-fill liquidation check requests a reduce-only close and halts."""
    position = account["position"]
    if not position.position_amt:
        return True
    direction = 1 if position.position_amt > 0 else -1
    stop = engine.s.get("position_stop")
    equity = number(account["coin"].asset("BTC").margin_balance)
    exposure = abs(position.position_amt)*market["spec"].contract_size/number(market["mark"])
    liq = number(position.liquidation_price)
    safe = (stop and liq > 0 and equity > 0 and exposure/equity <= D(3)
            and direction*(number(stop)-liq)/number(position.entry_price) >= D("0.05"))
    if safe:
        return True
    engine.s.put("halt", "aggressive_actual_liquidation_or_exposure_violation")
    engine.s.event("aggressive_margin_violation", {"contracts": str(position.position_amt),
                   "liquidation": str(liq), "stop": stop})
    await engine.submit("coinm", "aggressive_emergency:"+str(market["server_time_ms"]),
        {"symbol": "BTCUSD_PERP", "side": "SELL" if direction > 0 else "BUY",
         "quantity": str(abs(position.position_amt)), "reduce_only": True, "emergency": True, "_market": market})
    return False


async def execute(engine, account, markets, fees, equity):
    """One bounded step. Subsequent polls reconcile every IOC before another step."""
    from .engine import allocation_snapshot
    from .signals import alt_signal, coinm_signal
    from .filters import plan_order

    s, c = engine.s, engine.c
    alt, coin = alt_signal(markets["spot"]), coinm_signal(markets["coinm"])
    market = markets["coinm"]
    now = int(market["server_time_ms"])
    day = now//DAY
    from datetime import datetime, timezone
    date = datetime.fromtimestamp(now/1000, timezone.utc)
    month_key = f"{date.year:04d}-{date.month:02d}"
    coin_equity = number(account["coin"].asset("BTC").margin_balance)
    spot_equity = equity-coin_equity
    allocation = allocation_snapshot(c, spot_equity, coin_equity)
    baseline = s.get("aggressive_initial_equity")
    if baseline is None:
        baseline = str(equity)
        s.put("aggressive_initial_equity", baseline)
    killed = equity <= number(baseline)*number(c.kill_fraction) or s.get("aggressive_killed", False)
    if killed and not s.get("aggressive_killed", False):
        s.put("aggressive_killed", True)
        s.event("aggressive_kill", {"equity_btc": str(equity), "initial_btc": baseline})
    result = {"status": "READY", "equity_btc": str(equity), "btc_usd_reference": market["mark"],
              "alt_signal": alt, "coinm_signal": coin, "allocation": allocation,
              "aggressive_killed": killed, "new_entries_allowed": not killed}
    if not await validate_actual_position(engine, account, market):
        return {**result, "status": "BLOCKED", "action": "reduce_only_emergency_close"}
    wallet = s.get("wallet")
    if s.get("aggressive_transfer", {}).get("phase") == "PENDING":
        return {**result, "status": "BLOCKED", "reason": "transfer_outcome_requires_manual_reconciliation"}

    # Rotation may need several bounded IOC attempts; the prior asset sells first.
    if s.get("aggressive_alt_bar") != alt["bar"]:
        for symbol in c.symbols:
            if symbol == alt["symbol"]:
                continue
            held = number(wallet[symbol[:-3]])
            if held <= 0:
                continue
            origin_key = "aggressive_sell_origin:"+alt["bar"]+":"+symbol
            origin = number(s.get(origin_key, str(held)))
            if s.get(origin_key) is None:
                s.put(origin_key, str(held))
            if held <= origin*D("0.05"):
                continue
            attempts_key = "aggressive_attempts:sell:"+alt["bar"]+":"+symbol
            attempts = s.get(attempts_key, 0)
            if attempts >= 3:
                return {**result, "status": "BLOCKED", "reason": "spot_sell_retry_limit", "symbol": symbol}
            outcome = await engine.spot_trade(symbol, D(0), markets["spot"][symbol], fees["spot"][symbol],
                account, "aggressive_sell:"+alt["bar"]+":"+str(attempts))
            if outcome["status"] != "SUBMITTED":
                return {**result, "status": "BLOCKED", "reason": "spot_sell_not_executable", "symbol": symbol}
            s.put(attempts_key, attempts+1)
            s.event("aggressive_alt_rotation", {"symbol": symbol, "side": "SELL", "bar": alt["bar"]})
            return {**result, "action": outcome}
        s.put("aggressive_alt_bar", alt["bar"])

    position = account["position"]
    signed = 1 if position.position_amt > 0 else -1 if position.position_amt < 0 else 0
    coin_wait = None
    if signed and ((coin["direction"] and signed != coin["direction"]) or number(c.spot_fraction) == 1):
        flip = bool(coin["direction"]) and number(c.spot_fraction) < 1
        go, timing_info = timing.decide(s, c, coin, -signed, now) if flip else (True, None)
        if go:
            await reduce(engine, market, signed, abs(position.position_amt),
                         "aggressive_flip:"+coin["bar"]+":"+str(abs(position.position_amt)))
            s.event("aggressive_coin_flip", {"from": signed, "to": coin["direction"], "bar": coin["bar"],
                                             **({"timing": timing_info} if timing_info else {})})
            return {**result, "action": "coinm_reduce_only_flip"}
        coin_wait = timing_info
    elif coin["direction"] and signed == coin["direction"]:
        timing.clear(s)

    # Calendar-month rebalancing precedes the daily contract resize.
    if date.day == 1 and s.get("aggressive_rebalanced_month") != month_key and not killed:
        move = rebalance_amount(spot_equity, coin_equity, c.spot_fraction)
        if move:
            if c.rebalance_mode == "alert":
                s.event("aggressive_rebalance_alert", {"month": month_key,
                    "direction": "MAIN_CMFUTURE" if move > 0 else "CMFUTURE_MAIN", "amount_btc": str(abs(move))})
            else:
                from .transfer import rebalance_live
                outcome = await rebalance_live(engine, account, markets, fees, move, month_key)
                if outcome.get("status") != "ACKNOWLEDGED":
                    return {**result, "action": outcome}
                s.event("aggressive_rebalance", outcome)
                s.put("aggressive_rebalanced_month", month_key)
                return {**result, "action": outcome}
        s.put("aggressive_rebalanced_month", month_key)

    if (coin_wait is None and number(c.spot_fraction) < 1 and coin["direction"] and not signed and not killed
            and s.get("aggressive_blocked_side") != coin["direction"]):
        go, timing_info = timing.decide(s, c, coin, coin["direction"], now)
        if not go:
            coin_wait = timing_info
    if coin_wait is None and number(c.spot_fraction) < 1 and coin["direction"]:
        if not signed and not killed and s.get("aggressive_blocked_side") != coin["direction"]:
            if s.get("aggressive_blocked_side") is not None:
                s.put("aggressive_blocked_side", None)
            plan = coin_order_plan(c, market, coin["direction"], coin_equity,
                                   account["coin"].asset("BTC").available_balance, fees["coinm"])
            if plan["status"] == "READY":
                await engine.submit("coinm", "aggressive_entry:"+coin["bar"], {**plan, "_market": market})
                updated = await engine.v.account()
                if s.pending():
                    await engine.protect(updated)
                    return {**result, "status": "PENDING", "action": "coinm_entry_unresolved"}
                if not await validate_actual_position(engine, updated, market):
                    return {**result, "status": "BLOCKED", "action": "reduce_only_emergency_close"}
                await engine.protect(updated)
                s.event("aggressive_coin_entry", {"bar": coin["bar"], "plan": plan,
                                                  "timing": (s.get("timing_wait") or {})})
                return {**result, "action": "coinm_entry", "plan": plan}
            result["coinm_skip"] = plan
        elif signed and c.leverage_mode == "vol" and now//DAY != s.get("aggressive_resized_day"):
            s.put("aggressive_resized_day", now//DAY)
            sigma = volatility(market)
            lev = target_leverage(c, sigma)
            current = int(abs(position.position_amt))
            target = contract_count(lev, coin_equity, market["mark"], market["spec"].contract_size)
            target = min(target, contract_count(D(3), coin_equity, market["mark"], market["spec"].contract_size))
            if abs(target-current) >= max(1, round(.25*current)):
                if target < current and target >= 1:
                    await reduce(engine, market, signed, current-target,
                                 "aggressive_resize_down:"+str(day))
                    s.event("aggressive_coin_resize", {"from": current, "to": target, "day": day})
                    return {**result, "action": "coinm_resize_down"}
                if target > current and not killed:
                    plan = coin_order_plan(c, market, signed, coin_equity,
                        account["coin"].asset("BTC").available_balance, fees["coinm"], current=current,
                        stop=s.get("position_stop"), entry=position.entry_price)
                    if plan["status"] == "READY":
                        await engine.submit("coinm", "aggressive_resize_up:"+str(day),
                                            {**plan, "aggressive_resize": True, "_market": market})
                        updated = await engine.v.account()
                        if s.pending():
                            await engine.protect(updated)
                            return {**result, "status": "PENDING", "action": "coinm_resize_unresolved"}
                        if not await validate_actual_position(engine, updated, market):
                            return {**result, "status": "BLOCKED", "action": "reduce_only_emergency_close"}
                        s.event("aggressive_coin_resize", {"from": current, "to": plan["target_contracts"], "day": day})
                        return {**result, "action": "coinm_resize_up", "plan": plan}

    if coin_wait is not None:
        result["coinm_timing_wait"] = coin_wait
    winner = alt["symbol"]
    if winner and not killed:
        held_value = number(wallet[winner[:-3]])*number(markets["spot"][winner]["bid"])
        if spot_equity > 0 and held_value < spot_equity*D("0.95"):
            attempts_key = "aggressive_attempts:buy:"+alt["bar"]+":"+winner
            attempts = s.get(attempts_key, 0)
            if attempts < 3:
                outcome = await engine.spot_trade(winner, D(1), markets["spot"][winner],
                    fees["spot"][winner], account, "aggressive_buy:"+alt["bar"]+":"+str(attempts))
                if outcome["status"] == "SUBMITTED":
                    s.put(attempts_key, attempts+1)
                    s.event("aggressive_alt_rotation", {"symbol": winner, "side": "BUY", "bar": alt["bar"]})
                    return {**result, "action": outcome}
                result["spot_skip"] = outcome
            else:
                result["spot_skip"] = {"reason": "spot_buy_retry_limit"}
    s.event("evaluation", result)
    return result
