"""Intrabar quote triggers against closed 5m indicators and a closed 1h regime.

These rules are an explicit candidate, not a profitability claim. Missing or
stale data never becomes a neutral signal that liquidates an existing position.
"""
from decimal import Decimal as D
import time

from btc_spot.store import number
from .signals import closed, ema


def indicators(market, fee):
    now = int(market["server_time_ms"])
    age = time.time_ns() // 1_000_000 - int(market["received_at_ms"])
    if not -5000 <= age <= 15000:
        raise ValueError("Intraday quote is stale")
    bars = closed(market["klines"], now, 300_000, 100)
    trend = closed(market["trend_klines"], now, 3_600_000, 100)
    prices = [number(r[4]) for r in bars]
    trend_prices = [number(r[4]) for r in trend]
    fast, slow = ema(prices, 9), ema(prices, 21)
    tfast, tslow = ema(trend_prices, 20), ema(trend_prices, 50)
    bid, ask = number(market["bid"]), number(market["ask"])
    if min(bid, ask) <= 0 or ask < bid:
        raise ValueError("Invalid intraday book")
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid
    ranges = [max(number(bars[i][2])-number(bars[i][3]),
                  abs(number(bars[i][2])-prices[i-1]), abs(number(bars[i][3])-prices[i-1]))
              for i in range(86, 100)]
    atr = sum(ranges) / 14
    changes = [prices[i]-prices[i-1] for i in range(86, 100)]
    gains = sum(max(D(0), v) for v in changes)
    losses = sum(max(D(0), -v) for v in changes)
    rsi = 100*gains/(gains+losses) if gains+losses else D(50)
    volumes = [number(r[5]) for r in bars[-21:]]
    if min(volumes) < 0:
        raise ValueError("Invalid intraday volume")
    average_volume = sum(volumes[:-1]) / 20
    volume_ratio = volumes[-1]/average_volume if average_volume else D(0)
    high = max(number(r[2]) for r in bars[-12:])
    low = min(number(r[3]) for r in bars[-12:])
    raw_stop = 2*atr/mid
    # Cost gate is a volatility filter, not an estimated profit or an edge.
    cost = 2*number(fee) + spread + D(".0011")
    stop = max(D(".003"), raw_stop)
    reasons = []
    if spread > D(".001"):
        reasons.append("spread_above_10bps")
    if volume_ratio < D("1.2"):
        reasons.append("volume_below_1_2x")
    if raw_stop > D(".02"):
        reasons.append("atr_stop_above_2pct")
    if 2*raw_stop < 3*cost:
        reasons.append("volatility_too_small_for_cost")
    bullish = tfast > tslow and trend_prices[-1] > tslow
    bearish = tfast < tslow and trend_prices[-1] < tslow
    long_trigger = bullish and fast > slow and 50 <= rsi <= 75 and bid > high
    short_trigger = bearish and fast < slow and 25 <= rsi <= 50 and ask < low
    direction = (1 if long_trigger else -1 if short_trigger else 0) if not reasons else 0
    if not long_trigger and not short_trigger:
        reasons.append("waiting_for_trend_and_price_breakout")
    return {"bar": str(now//300_000*300_000), "direction": direction,
            "stop_fraction": str(stop), "exit_long": not bullish or bid < slow,
            "exit_short": not bearish or ask > slow,
            "score": str((mid/prices[-13]-1 + mid/prices[-49]-1)/2),
            "spread_bps": str(spread*10000), "rsi14": str(rsi),
            "volume_ratio": str(volume_ratio), "ema9": str(fast), "ema21": str(slow),
            "trend_ema20": str(tfast), "trend_ema50": str(tslow),
            "breakout_high": str(high), "breakout_low": str(low),
            "cost_fraction": str(cost), "reasons": reasons}


def signals(markets, fees):
    details = {s: indicators(m, fees["spot"][s]) for s, m in markets["spot"].items()}
    coin = indicators(markets["coinm"], fees["coinm"])
    epochs = {v["bar"] for v in details.values()} | {coin["bar"]}
    if len(epochs) != 1:
        raise ValueError("Intraday snapshots straddle different decision bars")
    eligible = [s for s, v in details.items() if v["direction"] == 1 and number(v["score"]) > 0]
    winner = max(sorted(eligible), key=lambda s: number(details[s]["score"])) if eligible else None
    alt = {"bar": coin["bar"], "symbol": winner, "details": details,
           "scores": {s: v["score"] for s, v in details.items()},
           "stop_fraction": details[winner]["stop_fraction"] if winner else None}
    return alt, coin


def exit_reason(entry, price, signed, now, signal, max_hold_seconds):
    """Profit/time/indicator exits; futures hard stops remain at the exchange."""
    if not entry or not entry.get("entered_at_ms"):
        raise ValueError("Intraday holding requires explicit entry metadata migration")
    origin = number(entry["price"])
    risk = number(entry["stop_fraction"])
    change = signed*(number(price)/origin-1)
    if change <= -risk:
        return "stop_loss"
    if change >= 2*risk:
        return "take_profit_2r"
    if now-int(entry["entered_at_ms"]) >= max_hold_seconds*1000:
        return "max_hold_time"
    if signal["exit_long" if signed > 0 else "exit_short"]:
        return "trend_exit"
    return None


async def execute(engine, account, markets, fees, equity, can_enter, allocation):
    """One action per evaluation, with persisted fills and per-venue cooldowns."""
    from .engine import coin_plan
    from .filters import plan_order
    c, s = engine.c, engine.s
    alt, coin = signals(markets, fees)
    now = int(markets["coinm"]["server_time_ms"])
    result = {"status": "READY", "strategy_mode": "intraday", "equity_btc": str(equity),
              "new_entries_allowed": can_enter, "btc_usd_reference": markets["coinm"]["mark"],
              "alt_signal": alt, "coinm_signal": coin, "allocation": allocation}

    def finish(**extra):
        result.update(extra)
        if extra or now-int(s.get("intraday_last_event_ms", 0)) >= 60000:
            s.event("evaluation", result)
            s.put("intraday_last_event_ms", now)
        return result

    sequence = str(s.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0])
    holdings = []
    for symbol in c.symbols:
        qty = number(s.get("wallet")[symbol[:-3]])
        if qty <= 0:
            continue
        plan = plan_order(btc_balance=qty, quote_balance=0, target_btc_fraction=0,
                          market=markets["spot"][symbol], fee_rate=fees["spot"][symbol])
        if plan["status"] == "READY":
            holdings.append(symbol)
        else:
            result.setdefault("dust", {})[symbol] = str(qty)
    for symbol in holdings:
        entry = s.get("alt_entry")
        if not entry or entry["symbol"] != symbol:
            raise ValueError("Intraday holding does not match committed entry")
        reason = exit_reason(entry, markets["spot"][symbol]["bid"], 1, now,
                             alt["details"][symbol], c.intraday_max_hold_seconds)
        if reason:
            action = await engine.spot_trade(symbol, D(0), markets["spot"][symbol], fees["spot"][symbol],
                                            account, "intraday_exit:"+sequence)
            return finish(action=action, exit_reason=reason)

    pos = account["position"]
    signed = 1 if pos.position_amt > 0 else -1 if pos.position_amt < 0 else 0
    if signed:
        reason = exit_reason(s.get("position_entry"), markets["coinm"]["mark"], signed, now,
                             coin, c.intraday_max_hold_seconds)
        if reason:
            market = markets["coinm"]
            price = market["spec"].round_price(number(market["bid" if signed > 0 else "ask"])*
                                              (D(".9997") if signed > 0 else D("1.0003")), "down" if signed > 0 else "up")
            await engine.submit("coinm", "intraday_exit:"+sequence, {"symbol": "BTCUSD_PERP",
                "side": "SELL" if signed > 0 else "BUY", "quantity": str(abs(pos.position_amt)),
                "price": str(price), "reduce_only": True, "_market": market})
            return finish(action="coinm_reduce_only_exit", exit_reason=reason)

    spot_wait = max(0, int(s.get("spot_cooldown_until", 0))-now)
    coin_wait = max(0, int(s.get("coin_cooldown_until", 0))-now)
    result["cooldown_ms"] = {"spot": spot_wait, "coinm": coin_wait}
    if can_enter and not signed and not coin_wait and s.get("coin_bar") != coin["bar"]:
        plan = coin_plan(c, markets["coinm"], coin, fees["coinm"],
                         number(account["coin"].asset("BTC").available_balance), equity)
        if plan["status"] == "READY":
            s.put("coin_bar", coin["bar"])
            await engine.submit("coinm", "intraday_entry:"+coin["bar"], {**plan, "_market": markets["coinm"]})
            await engine.protect(await engine.v.account())
            return finish(action="coinm_entry", plan=plan)
        result["coinm_skip"] = plan
    if can_enter and not holdings and not spot_wait and alt["symbol"] and s.get("alt_day") != alt["bar"]:
        symbol = alt["symbol"]
        btc = number(s.get("wallet")["BTC"])
        stop = number(alt["stop_fraction"])
        budget = min(number(c.spot_btc)*number(c.alt_max_fraction),
                     min(equity, c.total)*number(c.risk_fraction)/(stop+2*fees["spot"][symbol]+D(".001")))
        action = await engine.spot_trade(symbol, min(D(1), budget/btc) if btc else D(0),
                                        markets["spot"][symbol], fees["spot"][symbol], account,
                                        "intraday_entry:"+alt["bar"], stop_fraction=stop)
        if action["status"] == "SUBMITTED":
            s.put("alt_day", alt["bar"])
        return finish(action=action)
    return finish()
