"""Public market minimums and Decimal spot sizing for small BTC balances.

No private API, credentials, order submission, leverage changes or transfers.
The sizing result is a research calculation, not an exchange-approved order.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

D = Decimal
ZERO, ONE = D("0"), D("1")
SOURCES = {
    "spot_filters": "https://developers.binance.com/en/docs/products/spot/filters",
    "spot_public_market": "https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market",
    "spot_reference_price": "https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-reference-price",
    "coinm_public_market": "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/market-data",
    "usdm_public_market": "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data",
    "usdm_min_notional_exception": "https://www.binance.com/en-BH/support/announcement/detail/76719bbaeeb847bbac4daa2906fcdcc0",
    "usdm_2026_min_notional_change": "https://www.binance.com/en/support/announcement/detail/10999fd17dc045de801c0c78ab29e6fc",
    "usdm_market_notional_mark_price": "https://developers.binance.com/zh-CN/docs/products/derivatives-trading-usds-futures/common-definition",
}
HOSTS = {"spot": "https://api.binance.com", "coinm": "https://dapi.binance.com", "usdm": "https://fapi.binance.com"}
SYMBOLS = {"spot": {"BTCUSDT", "BTCUSDC"}, "coinm": {"BTCUSD_PERP"}, "usdm": {"BTCUSDT", "BTCUSDC"}}
PATHS = {
    "spot": {"/api/v3/time": set(), "/api/v3/exchangeInfo": {"symbol"},
             "/api/v3/ticker/bookTicker": {"symbol"}, "/api/v3/avgPrice": {"symbol"},
             "/api/v3/referencePrice": {"symbol"}},
    "coinm": {"/dapi/v1/time": set(), "/dapi/v1/exchangeInfo": set(), "/dapi/v1/ticker/bookTicker": {"symbol"}},
    "usdm": {"/fapi/v1/time": set(), "/fapi/v1/exchangeInfo": set(), "/fapi/v1/ticker/bookTicker": {"symbol"},
             "/fapi/v1/premiumIndex": {"symbol"}},
}


def decimal(value, name="value", *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, D) else D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def fmt(value):
    return format(value, "f") if isinstance(value, D) else value


def floor_step(value, step):
    value, step = decimal(value), decimal(step, "step", positive=True)
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(value, step):
    value, step = decimal(value), decimal(step, "step", positive=True)
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _flag(value):
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("Market application flags must be true or false")


@dataclass(frozen=True)
class MarketRules:
    min_qty: Decimal
    max_qty: Decimal | None
    step: Decimal
    min_notional: Decimal
    max_notional: Decimal | None
    avg_price_mins: tuple[int, ...]


def market_rules(filters) -> MarketRules:
    """Intersect LOT_SIZE with enabled MARKET_LOT_SIZE fields.

    A zero market lot field adds no restriction, so LOT_SIZE still governs.
    For unequal enabled step sizes use their exact decimal least common multiple.
    USD-M's MIN_NOTIONAL has `notional` without applyToMarket: apply it to a NEW
    market opening. This helper does not implement reduce-only futures orders.
    """
    by_type = {}
    for row in filters:
        name = row["filterType"]
        if name in by_type:
            raise ValueError("Duplicate symbol filter")
        by_type[name] = row
    if "LOT_SIZE" not in by_type:
        raise ValueError("LOT_SIZE is required")
    minima, maxima, steps = [], [], []
    for name in ("LOT_SIZE", "MARKET_LOT_SIZE"):
        row = by_type.get(name)
        if row is None:
            continue
        for key, dest in (("minQty", minima), ("maxQty", maxima), ("stepSize", steps)):
            value = decimal(row.get(key, "0"), key)
            if value:
                dest.append(value)
    if not steps:
        raise ValueError("At least one enabled quantity step is required")
    scale = 10 ** max(max(0, -x.normalize().as_tuple().exponent) for x in steps)
    step = D(math.lcm(*(int(x * scale) for x in steps))) / D(scale)
    minimum = max(minima, default=ZERO)
    maximum = min(maxima) if maxima else None
    notionals, max_notionals, windows = [], [], set()
    for name in ("MIN_NOTIONAL", "NOTIONAL"):
        row = by_type.get(name)
        if row is None:
            continue
        enabled_min = _flag(row.get("applyToMarket" if name == "MIN_NOTIONAL" else "applyMinToMarket", True))
        enabled_max = name == "NOTIONAL" and _flag(row.get("applyMaxToMarket", True))
        if enabled_min:
            notionals.append(decimal(row.get("minNotional", row.get("notional", "0")), "minNotional"))
        if enabled_max:
            upper = decimal(row.get("maxNotional", "0"), "maxNotional")
            if upper:
                max_notionals.append(upper)
        if enabled_min or enabled_max:
            mins = int(row.get("avgPriceMins", 0))
            if mins < 0:
                raise ValueError("Invalid average price window")
            windows.add(mins)
    low = max(notionals, default=ZERO)
    high = min(max_notionals) if max_notionals else None
    if maximum is not None and maximum < minimum or high is not None and high < low:
        raise ValueError("Inconsistent market filters")
    return MarketRules(minimum, maximum, step, low, high, tuple(sorted(windows)))


def minimum_market_quantity(rules: MarketRules, notional_price):
    price = decimal(notional_price, "notional_price", positive=True)
    quantity = ceil_step(max(rules.min_qty, rules.min_notional / price, rules.step), rules.step)
    if rules.max_qty is not None and quantity > rules.max_qty or rules.max_notional is not None and quantity * price > rules.max_notional:
        raise ValueError("No quantity satisfies these market filters at this price")
    return quantity


def size_spot_order(*, btc_balance, quote_balance, target_btc_fraction, bid, ask,
                    filters, fee_rate=D(".001"), slippage_bps=D("3"), reference_price=None,
                    fee_asset_mode="received"):
    """Size one self-funded base-quantity MARKET rebalance, never force a minimum.

    Returns JSON-safe strings. Default fees are deducted in the RECEIVED asset:
    BUY pays BTC and SELL pays quote. Optional `quote` mode adds buy commission
    to quote spending. A BNB fee discount is not assumed by either mode.
    target_btc_fraction is the desired BTC share of POST-FEE equity at book mid.
    reference_price should be Binance's reference price, or the applicable VWAP.
    Without it, bid/ask plus slip provide an indicative conservative price band;
    this cannot certify acceptance against an unknown exchange reference price.
    Balances must be free balances reserved for this strategy, excluding other
    orders and the reserve wallet. No borrowing or reserve draw is implemented.
    """
    with localcontext() as context:
        context.prec = 40
        btc = decimal(btc_balance, "btc_balance")
        quote = decimal(quote_balance, "quote_balance")
        target = decimal(target_btc_fraction, "target_btc_fraction")
        bid, ask = decimal(bid, "bid", positive=True), decimal(ask, "ask", positive=True)
        fee, slip = decimal(fee_rate, "fee_rate"), decimal(slippage_bps, "slippage_bps") / 10000
        if target > 1 or fee >= 1 or slip >= 1 or bid > ask or fee_asset_mode not in {"received", "quote"}:
            raise ValueError("Invalid target, cost, slippage or crossed book")
        rules = market_rules(filters)
        mid = (bid + ask) / 2
        low_price, high_price = bid * (1 - slip), ask * (1 + slip)
        if reference_price is not None:
            low_price = high_price = decimal(reference_price, "reference_price", positive=True)
        wealth = btc * mid + quote
        delta = target * wealth - btc * mid
        side = "BUY" if delta > 0 else "SELL" if delta < 0 else "NONE"
        execution = ask * (1 + slip) if side == "BUY" else bid * (1 - slip)
        result = {"status": "SKIP", "side": side, "quantity": "0", "execution_price": fmt(execution),
                  "notional": "0", "fee_quote": "0", "fee_btc": "0", "fee_quote_equivalent": "0", "quote_delta": "0", "btc_delta": "0",
                  "btc_after": fmt(btc), "quote_after": fmt(quote), "reason": "already_at_target",
                  "reference_price_confirmed": reference_price is not None,
                  "fee_asset_assumption": fee_asset_mode.upper(), "step": fmt(rules.step),
                  "minimum_quantity_at_reference": fmt(minimum_market_quantity(rules, low_price))}
        if not delta:
            return result
        received_fee_buy = side == "BUY" and fee_asset_mode == "received"
        unit_base = 1 - fee if received_fee_buy else ONE
        unit_cash = execution if received_fee_buy else execution * (1 + fee if side == "BUY" else 1 - fee)
        denominator = unit_base * mid * (1 - target) + target * unit_cash
        wanted = abs(delta) / denominator
        capacity = quote / unit_cash if side == "BUY" else btc
        caps = [wanted, capacity]
        if rules.max_qty is not None:
            caps.append(rules.max_qty)
        if rules.max_notional is not None:
            caps.append(rules.max_notional / high_price)
        # Account-specific MAX_POSITION needs locked balances/open buy orders;
        # this calculation deliberately does not pretend to have that data.
        quantity = floor_step(min(caps), rules.step)
        if quantity < rules.min_qty or not quantity:
            result["reason"] = "below_minimum_quantity"
            return result
        if quantity * low_price < rules.min_notional:
            result["reason"] = "below_minimum_notional_no_upsize"
            return result
        notional = quantity * execution
        commission_btc = quantity * fee if received_fee_buy else ZERO
        commission_quote = ZERO if received_fee_buy else notional * fee
        change_btc = quantity - commission_btc if side == "BUY" else -quantity
        change_quote = -(notional + commission_quote) if side == "BUY" else notional - commission_quote
        after_btc, after_quote = btc + change_btc, quote + change_quote
        if after_btc < 0 or after_quote < 0:
            raise AssertionError("Sizing would borrow an asset")
        result.update(status="READY", quantity=fmt(quantity), notional=fmt(notional), fee_quote=fmt(commission_quote),
                      fee_btc=fmt(commission_btc), fee_quote_equivalent=fmt(commission_quote + commission_btc * execution),
                      quote_delta=fmt(change_quote), btc_delta=fmt(change_btc), btc_after=fmt(after_btc),
                      quote_after=fmt(after_quote), reason="within_public_filters_and_free_balances")
        return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Public API redirect refused")


class PublicClient:
    def __init__(self, timeout=12):
        self.opener = build_opener(_NoRedirect())
        self.timeout = timeout

    def get(self, market, path, params=None):
        params = params or {}
        if market not in PATHS or path not in PATHS[market] or not set(params) <= PATHS[market][path]:
            raise ValueError("Only explicitly allowlisted public GETs are supported")
        if "symbol" in params and params["symbol"] not in SYMBOLS[market]:
            raise ValueError("Unsupported public symbol")
        if PATHS[market][path] == {"symbol"} and set(params) != {"symbol"}:
            raise ValueError("An explicitly supported symbol is required")
        url = HOSTS[market] + path + ("?" + urlencode(params) if params else "")
        request = Request(url, method="GET", headers={"User-Agent": "btc-lab-market-fit-public/1"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(20_000_001)
        except HTTPError as exc:
            if market == "spot" and path == "/api/v3/referencePrice" and exc.code == 400:
                payload = json.loads(exc.read(10000))
                if payload.get("code") == -2043:
                    return payload
            raise RuntimeError(f"Public HTTP request failed ({exc.code})") from exc
        if len(body) > 20_000_000:
            raise ValueError("Public response exceeds limit")
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("code", 0) < 0:
            if not (path == "/api/v3/referencePrice" and payload["code"] == -2043):
                raise RuntimeError("Public API error")
        return payload


def _book(payload, symbol):
    if isinstance(payload, list):
        payload = next(row for row in payload if row["symbol"] == symbol)
    if payload.get("symbol") != symbol:
        raise ValueError("Unexpected book symbol")
    bid, ask = decimal(payload["bidPrice"], positive=True), decimal(payload["askPrice"], positive=True)
    if bid > ask:
        raise ValueError("Crossed public book")
    for name in ("bidQty", "askQty"):
        decimal(payload[name], name, positive=True)
    return payload


def _stringify(value):
    if isinstance(value, D):
        return fmt(value)
    if isinstance(value, dict):
        return {k: _stringify(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_stringify(x) for x in value]
    return value


def _comparison(raw, trading_btc, whole_btc, spot_fees, futures_fee, slip_bps):
    """Capacity is a hypothetical filter/margin calculation, not an allocation."""
    results = []
    slip = slip_bps / 10000
    for key, item in raw["markets"].items():
        market, symbol = key.split(":")
        meta, book = item["spec"], item["book"]
        bid, ask = D(book["bidPrice"]), D(book["askPrice"])
        midpoint = (bid + ask) / 2
        rules = market_rules(meta["filters"])
        if market == "spot":
            fee = spot_fees[symbol]
            reference = item["reference"].get("referencePrice")
            if reference is not None:
                notional_price = decimal(reference, positive=True)
                reference_kind = "exchange_reference_price"
            else:
                average = item["average"]
                if len(rules.avg_price_mins) != 1 or rules.avg_price_mins[0] != int(average["mins"]):
                    raise ValueError("Available average price does not match active market notional filters")
                notional_price = decimal(average["price"], positive=True)
                reference_kind = "exchange_avg_price"
            minimum_qty = minimum_market_quantity(rules, notional_price)
            step_notional = rules.step * midpoint
            min_buy = minimum_qty * ask * (1 + slip)
            min_sell = minimum_qty * bid * (1 - slip)
            minimum_btc = minimum_qty
            budgets = {}
            for name, btc in (("trading_budget", trading_btc), ("whole_wallet_comparison_only", whole_btc)):
                sell = size_spot_order(btc_balance=btc, quote_balance=0, target_btc_fraction=0,
                    bid=bid, ask=ask, filters=meta["filters"], fee_rate=fee, slippage_bps=slip_bps, reference_price=notional_price)
                # Conversion is itself a step-quantized order. BTC dust remains
                # BTC and is not invented as spendable quote collateral.
                quote_after_conversion = D(sell["quote_after"])
                buy = size_spot_order(btc_balance=sell["btc_after"], quote_balance=quote_after_conversion, target_btc_fraction=1,
                    bid=bid, ask=ask, filters=meta["filters"], fee_rate=fee, slippage_bps=slip_bps, reference_price=notional_price)
                budgets[name] = {"btc_budget": btc, "midpoint_quote_value": btc * midpoint,
                    "hypothetical_quote_after_btc_conversion": quote_after_conversion,
                    "btc_dust_after_conversion_not_quote_collateral": sell["btc_after"],
                    "all_to_btc_from_converted_quote": buy, "all_to_quote_from_btc": sell,
                    "min_order_pct_of_btc_budget": minimum_qty / btc * 100,
                    "increment_pct_of_btc_budget": rules.step / btc * 100}
            row = {"market": market, "symbol": symbol, "quantity_unit": "BTC", "minimum_market_quantity": minimum_qty,
                   "minimum_buy_notional_with_slip": min_buy, "minimum_sell_notional_with_slip": min_sell,
                   "minimum_notional_filter": rules.min_notional, "notional_reference_price": notional_price,
                   "notional_reference_kind": reference_kind, "minimum_btc_equivalent": minimum_btc,
                   "quantity_increment": rules.step, "increment_quote_value_at_mid": step_notional,
                   "fee_rate": fee, "fee_asset_mode": "received", "budgets": budgets}
        else:
            fee = futures_fee
            if market == "coinm":
                face = decimal(meta["contractSize"], positive=True)
                minimum_qty = ceil_step(max(rules.min_qty, rules.step), rules.step)
                minimum_notional = minimum_qty * face
                increment_notional = rules.step * face
                minimum_btc = minimum_notional / midpoint
                unit_btc_cost = face / (bid * (1 - slip))
                reference_kind, notional_price = "fixed_USD_contract_face", None
            else:
                notional_price = decimal(item["premium"]["markPrice"], positive=True)
                minimum_qty = minimum_market_quantity(rules, notional_price)
                minimum_notional = minimum_qty * ask * (1 + slip)
                increment_notional = rules.step * midpoint
                minimum_btc = minimum_qty
                reference_kind = "exchange_mark_price"
            budgets = {}
            for name, btc in (("trading_budget", trading_btc), ("whole_wallet_comparison_only", whole_btc)):
                if market == "coinm":
                    budget_quote = btc * midpoint
                    one_unit = unit_btc_cost
                    funding_balance = btc
                else:
                    spot_item = raw["markets"]["spot:" + symbol]
                    spot = spot_item["book"]
                    spot_ref = spot_item["reference"].get("referencePrice") or spot_item["average"]["price"]
                    conversion = size_spot_order(btc_balance=btc, quote_balance=0, target_btc_fraction=0,
                        bid=spot["bidPrice"], ask=spot["askPrice"], filters=spot_item["spec"]["filters"],
                        fee_rate=spot_fees[symbol], slippage_bps=slip_bps, reference_price=spot_ref)
                    budget_quote = D(conversion["quote_after"])
                    funding_balance = budget_quote
                    one_unit = ask * (1 + slip)
                capacities = {}
                for leverage in (1, 3):
                    # Two taker fees reserved, including the eventual exit.
                    capacity = floor_step(funding_balance / (one_unit * (ONE / leverage + 2 * fee)), rules.step)
                    if rules.max_qty is not None:
                        capacity = min(capacity, floor_step(rules.max_qty, rules.step))
                    if capacity < minimum_qty:
                        capacity = ZERO
                    capacities[str(leverage)] = capacity
                budgets[name] = {"btc_budget": btc, "quote_budget_or_conversion_value": budget_quote,
                    "btc_dust_after_conversion_not_quote_collateral": conversion["btc_after"] if market == "usdm" else "0",
                    "min_notional_pct_of_budget": minimum_notional / budget_quote * 100 if budget_quote else None,
                    "increment_notional_pct_of_budget": increment_notional / budget_quote * 100 if budget_quote else None,
                    "hypothetical_capacity_by_initial_margin_leverage_with_two_fees": capacities}
            row = {"market": market, "symbol": symbol, "quantity_unit": "contracts" if market == "coinm" else "BTC",
                   "minimum_market_opening_quantity": minimum_qty, "minimum_opening_notional_quote": minimum_notional,
                   "minimum_btc_equivalent_at_book_mid": minimum_btc, "quantity_increment": rules.step,
                   "increment_quote_value_at_mid": increment_notional, "minimum_notional_filter": rules.min_notional,
                   "notional_reference_kind": reference_kind, "notional_reference_price": notional_price,
                   "fee_rate_assumption": fee, "budgets": budgets,
                   "opening_only_note": "Futures opening minimum is not a reduce-only closing restriction; exchange min-quantity rules still need separate checks."}
        row.update(bid=bid, ask=ask, bid_qty=D(book["bidQty"]), ask_qty=D(book["askQty"]),
                   rules=asdict(rules), snapshot_received_at_utc=item["received_at_utc"],
                   min_order_within_best_bid_qty=minimum_qty <= D(book["bidQty"]),
                   min_order_within_best_ask_qty=minimum_qty <= D(book["askQty"]))
        results.append(row)
    return _stringify(results)


def scan(output, *, trading_btc=D(".003"), whole_btc=D(".00412273"), spot_fees=None,
         futures_fee=D(".0005"), slippage_bps=D("3"), client=None):
    output = Path(output)
    for name in ("public_snapshot.json", "market_fit_report.json",
                 "exchange_info_BTCUSDT_spot.json", "exchange_info_BTCUSDC_spot.json"):
        if (output / name).exists():
            raise FileExistsError(f"Preserving existing research artifact: {output / name}")
    trading_btc, whole_btc = decimal(trading_btc, positive=True), decimal(whole_btc, positive=True)
    if trading_btc > whole_btc:
        raise ValueError("Trading budget cannot include more than the whole balance")
    fees = spot_fees or {"BTCUSDT": D(".001"), "BTCUSDC": D(".001")}
    fees = {key: decimal(fees[key], "fee") for key in SYMBOLS["spot"]}
    futures_fee, slippage_bps = decimal(futures_fee), decimal(slippage_bps)
    if any(f >= 1 for f in fees.values()) or futures_fee >= 1 or slippage_bps >= 10000:
        raise ValueError("Invalid costs")
    client = client or PublicClient()
    raw = {"read_only": True, "markets": {}, "server_times": {}}
    for market in ("spot", "coinm", "usdm"):
        prefix = "/api/v3" if market == "spot" else "/dapi/v1" if market == "coinm" else "/fapi/v1"
        exchange = client.get(market, prefix + "/exchangeInfo") if market != "spot" else None
        for symbol in sorted(SYMBOLS[market]):
            metadata = exchange or client.get(market, prefix + "/exchangeInfo", {"symbol": symbol})
            matched = [s for s in metadata["symbols"] if s["symbol"] == symbol]
            if len(matched) != 1:
                raise ValueError("Requested contract metadata missing")
            meta = matched[0]
            if meta.get("status", meta.get("contractStatus")) != "TRADING" or meta.get("baseAsset") != "BTC":
                raise ValueError("Instrument is not an active BTC market")
            if market != "spot" and meta.get("contractType") != "PERPETUAL":
                raise ValueError("Expected perpetual futures contract")
            book = _book(client.get(market, prefix + "/ticker/bookTicker", {"symbol": symbol}), symbol)
            item = {"spec": meta, "book": book, "received_at_utc": datetime.now(timezone.utc).isoformat()}
            if market == "spot":
                item["reference"] = client.get(market, prefix + "/referencePrice", {"symbol": symbol})
                item["average"] = client.get(market, prefix + "/avgPrice", {"symbol": symbol})
            elif market == "usdm":
                item["premium"] = client.get(market, prefix + "/premiumIndex", {"symbol": symbol})
            raw["markets"][market + ":" + symbol] = item
        raw["server_times"][market] = client.get(market, prefix + "/time")
    report = {"report_version": 2, "generated_utc": datetime.now(timezone.utc).isoformat(), "orders_enabled": False,
        "trading_budget_btc": fmt(trading_btc), "whole_balance_reference_btc": fmt(whole_btc),
        "reserve_excluded_from_trading_btc": fmt(whole_btc - trading_btc),
        "configured_spot_fees": _stringify(fees), "futures_fee_assumption": fmt(futures_fee),
        "slippage_bps_assumption": fmt(slippage_bps),
        "markets": _comparison(raw, trading_btc, whole_btc, fees, futures_fee, slippage_bps),
        "limitations": ["Sizing is a public-filter calculation, not an accepted order or trade permission check.",
            "Book, reference, average and mark prices are sequential snapshots and can move before execution.",
            "No private balances, locked balances, orders, MAX_ASSET/myFilters or account leverage brackets are queried.",
            "Spot fees are supplied assumptions; received-asset accounting, no BNB discount.",
            "USD-M requires stablecoin collateral; hypothetical BTC conversion cost is included but no conversion occurs.",
            "USDT, USDC and USD are compared at their own BTC quotes; they are not guaranteed equivalents.",
            "Futures capacity reserves two fees but not unknown funding, maintenance changes, losses or liquidation buffers.",
            "Whole-wallet columns show scale only. The reserve is not spendable by the trading-budget examples.",
            "Futures minimum opening notional is not applied to reduce-only closures in this comparison."],
        "sources": SOURCES}
    output.mkdir(parents=True, exist_ok=True)
    artifacts = [("public_snapshot.json", raw), ("market_fit_report.json", report)]
    for symbol in sorted(SYMBOLS["spot"]):
        item = raw["markets"]["spot:" + symbol]
        artifacts.append((f"exchange_info_{symbol}_spot.json", {
            "symbols": [item["spec"]], "retrieved_utc": item["received_at_utc"],
            "public_source": f"https://api.binance.com/api/v3/exchangeInfo?symbol={symbol}"}))
    for name, data in artifacts:
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("btc_lab/state/market_fit_20260924"))
    parser.add_argument("--trading-btc", type=D, default=D(".003"))
    parser.add_argument("--whole-btc", type=D, default=D(".00412273"))
    parser.add_argument("--usdt-fee", type=D, default=D(".001"))
    parser.add_argument("--usdc-fee", type=D, default=D(".001"))
    args = parser.parse_args(argv)
    report = scan(args.output, trading_btc=args.trading_btc, whole_btc=args.whole_btc,
                  spot_fees={"BTCUSDT": args.usdt_fee, "BTCUSDC": args.usdc_fee})
    print(json.dumps({"output": str(args.output), "generated_utc": report["generated_utc"],
        "markets": [{"market": r["market"], "symbol": r["symbol"], "qty_step": r["quantity_increment"],
            "minimum_qty": r.get("minimum_market_quantity", r.get("minimum_market_opening_quantity")),
            "minimum_notional": r.get("minimum_buy_notional_with_slip", r.get("minimum_opening_notional_quote"))} for r in report["markets"]]}, indent=2))


if __name__ == "__main__":
    main()
