"""IOC filter checks for actual base/quote assets, including account filters."""
from decimal import Decimal
from btc_spot.store import number
from btc_spot.engine import plan_order as _plan_order


def plan_order(**kwargs):
    """Reuse numerical sizing while labelling the actual base and BTC quote correctly."""
    plan = _plan_order(**kwargs)
    market = kwargs["market"]
    if "maximum_btc_debit_including_fee" in plan:
        plan["maximum_base_debit_including_fee"] = plan.pop("maximum_btc_debit_including_fee")
    plan["fee_budget_assumption"] = market["base_asset"]+"_or_"+market["quote_asset"]+"_worst_case"
    return plan


def account_balances(account):
    result = {}
    for row in account["balances"]:
        asset = row["asset"]
        free, locked = number(row["free"]), number(row["locked"])
        if asset in result or min(free, locked) < 0:
            raise ValueError("Invalid account balances")
        result[asset] = {"free": free, "locked": locked, "total": free+locked}
    return result


def validate_plan_filters(market, sized, balances, mode="live"):
    if isinstance(balances, dict) and isinstance(balances.get("balances"), list):
        balances = account_balances(balances)
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
            if item.get("symbol") == market["symbol"]:
                symbol_filters.extend(item["filters"])
        elif item.get("symbol", market["symbol"]) == market["symbol"]:
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
            if sized["side"] == "BUY" and balances[market["base_asset"]]["total"] + quantity > number(row["maxPosition"]):
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
        amount = quantity if asset == market["base_asset"] else quantity * effective_price if asset == market["quote_asset"] else Decimal(0)
        if amount > limit:
            raise ValueError(f"MAX_ASSET order limit exceeded for {asset}")
    return {"status": "VALID", "reason": "known_account_and_limit_filters_checked"}

