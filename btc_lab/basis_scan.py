"""Read-only BTC inverse-futures carry research. No credentials or order API.

Run: python -m btc_lab.basis_scan
Each run defaults to a new UTC timestamped directory. Existing snapshots are
never overwritten, including when --output explicitly names a directory.
Numbers are conditional scenarios, never a promised return or an order signal.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://dapi.binance.com"
DAY_MS = 86_400_000
GET_PATHS = frozenset({"/dapi/v1/time", "/dapi/v1/exchangeInfo", "/dapi/v1/depth",
                       "/dapi/v1/premiumIndex", "/dapi/v1/fundingRate"})
SOURCES = {
    "market_data": "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/market-data",
    "settlement": "https://www.binance.com/en-AE/support/faq/detail/a3401595e1734084959c61491bc0dbe3",
    "funding": "https://www.binance.com/en/support/faq/detail/360033525031",
    "liquidation": "https://www.binance.com/en-AE/support/faq/detail/ceccfcfb4e3a45e3b48b0b1bb1a8ae46",
    "fees": "https://www.binance.com/en/fee/deliveryFee",
}


def positive(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


def public_get(path: str, **params):
    if path not in GET_PATHS:
        raise ValueError("Only the explicit public market-data GET allowlist is available")
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "btc-lab-public-research/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def inverse_execution_price(levels: list, quantity: float) -> float:
    """Harmonic price preserves sum(qty * face / price) for inverse contracts."""
    remaining = positive(quantity, "quantity")
    reciprocal_value = 0.0
    for row in levels:
        price, size = positive(row[0], "price"), positive(row[1], "book size")
        take = min(size, remaining)
        reciprocal_value += take / price
        remaining -= take
        if remaining <= 1e-10:
            return quantity / reciprocal_value
    raise ValueError("Insufficient displayed depth")


def inverse_pair_pnl(notional: float, long_entry: float, short_entry: float,
                     long_exit: float, short_exit: float) -> float:
    for value, name in ((notional, "notional"), (long_entry, "long_entry"),
                        (short_entry, "short_entry"), (long_exit, "long_exit"), (short_exit, "short_exit")):
        positive(value, name)
    return notional * (1 / long_entry - 1 / long_exit + 1 / short_exit - 1 / short_entry)


def scenario(notional: float, long_entry: float, short_entry: float,
             terminal_index: float, perp_side: int, remaining_days: float,
             funding_per_day: float, fee_rate: float, exit_basis_bps: float = 0.0) -> dict:
    """Settlement of dated leg + simultaneous perp close, conditional price path.

    Future funding BTC is approximated at a CONSTANT terminal_index for all
    settlements. Historical rate extrapolation is an illustrative stress, not
    a prediction. Positive exit_basis means perp trades above settlement index.
    """
    if perp_side not in (-1, 1):
        raise ValueError("perp_side must be -1 or 1")
    positive(terminal_index, "terminal_index")
    if remaining_days < 0 or not math.isfinite(remaining_days):
        raise ValueError("remaining_days must be nonnegative")
    if fee_rate < 0 or not math.isfinite(fee_rate) or not math.isfinite(funding_per_day):
        raise ValueError("invalid fee/funding rate")
    perp_exit = positive(terminal_index * (1 + exit_basis_bps / 10000), "perp_exit")
    long_exit, short_exit = ((perp_exit, terminal_index) if perp_side == 1 else (terminal_index, perp_exit))
    gross = inverse_pair_pnl(notional, long_entry, short_entry, long_exit, short_exit)
    fees = fee_rate * notional * (1 / long_entry + 1 / short_entry + 1 / long_exit + 1 / short_exit)
    funding = -perp_side * notional / terminal_index * funding_per_day * remaining_days
    return {"gross_btc": gross, "fee_btc": fees, "funding_btc": funding,
            "net_btc": gross - fees + funding, "terminal_index": terminal_index,
            "exit_perp_basis_bps": exit_basis_bps, "funding_per_day": funding_per_day}


def funding_summary(rows: list[dict], now_ms: int, days: int, notional: float = 100) -> dict:
    selected = [r for r in rows if now_ms - days * DAY_MS <= int(r["fundingTime"]) <= now_ms]
    rates = [float(r["fundingRate"]) for r in selected]
    if any(not math.isfinite(x) for x in rates):
        raise ValueError("Nonfinite funding rate")
    known = [r for r in selected if r.get("markPrice") and float(r["markPrice"]) > 0]
    exact_paid = sum(notional / float(r["markPrice"]) * float(r["fundingRate"]) for r in known)
    times = sorted(set(int(r["fundingTime"]) for r in selected))
    return {"days": days, "count": len(selected), "rate_sum": sum(rates),
            "rate_per_day": sum(rates) / days,
            "annualized_historical_rate_not_forecast": sum(rates) / days * 365,
            "positive_count": sum(x > 0 for x in rates),
            "negative_count": sum(x < 0 for x in rates),
            "long_paid_btc_per_100usd_with_known_marks": exact_paid,
            "missing_marks": len(selected) - len(known),
            "max_event_gap_hours": max(((b-a) / 3600000 for a, b in zip(times, times[1:])), default=0),
            "first_event_ms": times[0] if times else None, "last_event_ms": times[-1] if times else None}


def quantity_rules(symbol: dict) -> tuple[float, float, float]:
    filters = {f["filterType"]: f for f in symbol["filters"]}
    lot, market = filters["LOT_SIZE"], filters.get("MARKET_LOT_SIZE", filters["LOT_SIZE"])
    return max(float(lot["minQty"]), float(market["minQty"])), max(float(lot["stepSize"]), float(market["stepSize"])), min(float(lot["maxQty"]), float(market["maxQty"]))


def margin_capacity(long_entry: float, short_entry: float, face: float, equity: float,
                    leverage: float, reserve: float, fee_rate: float, step: float = 1) -> int:
    for v, n in ((long_entry, "long_entry"), (short_entry, "short_entry"), (face, "face"),
                 (equity, "equity"), (leverage, "leverage"), (step, "step")):
        positive(v, n)
    if not 0 <= reserve < 1 or not 0 <= fee_rate < 1:
        raise ValueError("Invalid reserve or fee")
    per_pair = face * (1 / long_entry + 1 / short_entry) * (1 / leverage + fee_rate)
    return int(math.floor(equity * (1 - reserve) / per_pair / step) * step)


def build_report(raw: dict, equity: float = .007, leverage: float = 3,
                 reserve: float = .25, fee_rate: float = .0005) -> dict:
    now = int(raw["server_time"]["serverTime"])
    symbols = {s["symbol"]: s for s in raw["symbols"]}
    perp = next(s for s in symbols.values() if s["contractType"] == "PERPETUAL")
    dated = sorted((s for s in symbols.values() if s["contractType"] != "PERPETUAL"), key=lambda s: s["deliveryDate"])
    premium = {x["symbol"]: x for x in raw["premium"]}
    index = positive(premium[perp["symbol"]]["indexPrice"], "index")
    funding = {str(d): funding_summary(raw["funding"], now, d) for d in (7, 30, 90)}
    pairs = []
    for future in dated:
        days = (int(future["deliveryDate"]) - now) / DAY_MS
        if days <= 0 or float(future["contractSize"]) != float(perp["contractSize"]):
            continue
        for side in (1, -1):
            long, short = (perp, future) if side == 1 else (future, perp)
            long_book, short_book = raw["depth"][long["symbol"]], raw["depth"][short["symbol"]]
            long_rules, short_rules = quantity_rules(long), quantity_rules(short)
            qty = max(long_rules[0], short_rules[0])
            step = max(long_rules[1], short_rules[1])
            if abs(qty / step - round(qty / step)) > 1e-9:
                raise ValueError("Incompatible contract quantity steps")
            lp = inverse_execution_price(long_book["asks"], qty)
            sp = inverse_execution_price(short_book["bids"], qty)
            face = float(perp["contractSize"])
            notional = qty * face
            ages = [(now - int(b["T"])) / 1000 for b in (long_book, short_book)]
            quote_skew = abs(int(long_book["T"]) - int(short_book["T"])) / 1000
            capacity = min(margin_capacity(lp, sp, face, equity, leverage, reserve, fee_rate, step),
                           int(long_rules[2]), int(short_rules[2]))
            capacity_depth_ok = False
            capacity_cases = {}
            if capacity >= qty:
                try:
                    cap_lp = inverse_execution_price(long_book["asks"], capacity)
                    cap_sp = inverse_execution_price(short_book["bids"], capacity)
                    capacity_depth_ok = margin_capacity(cap_lp, cap_sp, face, equity, leverage, reserve, fee_rate, step) >= capacity
                except ValueError:
                    pass
            cases = {}
            rates = {"zero_funding": 0.0, **{f"repeat_last_{d}d_rate": funding[str(d)]["rate_per_day"] for d in (7, 30, 90)},
                     "repeat_current_funding_quote_assuming_8h": float(premium[perp["symbol"]]["lastFundingRate"]) * 3,
                     "positive_0_01pct_each_8h": .0003, "negative_0_01pct_each_8h": -.0003}
            for label, daily_rate in rates.items():
                case = scenario(notional, lp, sp, index, side, days, daily_rate, fee_rate)
                case["account_return_pct_at_min_quantity"] = case["net_btc"] / equity * 100
                cases[label] = case
                if capacity_depth_ok:
                    cap_case = scenario(capacity * face, cap_lp, cap_sp, index, side, days, daily_rate, fee_rate)
                    cap_case["account_return_pct"] = cap_case["net_btc"] / equity * 100
                    capacity_cases[label] = cap_case
            cases["repeat_last_30d_rate_double_fees"] = scenario(
                notional, lp, sp, index, side, days, funding["30"]["rate_per_day"], fee_rate * 2)
            cases["repeat_last_30d_rate_double_fees"]["account_return_pct_at_min_quantity"] = cases["repeat_last_30d_rate_double_fees"]["net_btc"] / equity * 100
            stress = []
            for price_mult in (.5, 1, 2):
                for exit_bps in (-50, 0, 50):
                    result = scenario(notional, lp, sp, index * price_mult, side, days,
                                      funding["30"]["rate_per_day"], fee_rate, exit_bps)
                    result["price_multiplier"] = price_mult
                    stress.append(result)
            gross_convergence = inverse_pair_pnl(notional, lp, sp, index, index)
            no_funding = cases["zero_funding"]
            # Solve net = gross - fees - side*N/index*daily_rate*days = 0.
            break_even_rate = no_funding["net_btc"] * index / (side * notional * days)
            initial_margin = notional * (1 / lp + 1 / sp) / leverage
            pairs.append({"long": long["symbol"], "short": short["symbol"], "perp_side": side,
                          "delivery_date_utc": datetime.fromtimestamp(future["deliveryDate"] / 1000, timezone.utc).isoformat(),
                          "days_to_delivery": days, "minimum_quantity_each": qty,
                          "usd_notional_each": notional, "long_entry_ask": lp, "short_entry_bid": sp,
                          "entry_inverse_basis_btc": gross_convergence,
                          "initial_margin_estimate_btc": initial_margin,
                          "entry_fees_btc": fee_rate * notional * (1 / lp + 1 / sp),
                          "capacity_pairs_with_reserve_not_recommendation": capacity,
                          "capacity_depth_and_margin_ok": capacity_depth_ok,
                          "scenarios_at_capacity_not_allocation_recommendation": capacity_cases,
                          "depth_event_age_seconds": ages, "depth_cross_leg_skew_seconds": quote_skew,
                          "quotes_fresh": all(0 <= age <= 15 for age in ages) and quote_skew <= 5,
                          "break_even_funding_rate_per_day_constant_index": break_even_rate,
                          "break_even_funding_rate_per_8h_constant_index": break_even_rate / 3,
                          "isolated_bankruptcy_ignoring_maintenance_fees_and_funding": {
                              "long_price": lp / (1 + 1 / leverage),
                              "short_price": sp / (1 - 1 / leverage) if leverage > 1 else None,
                              "warning": "Not liquidation prices. Isolated legs cannot automatically use each other's unrealized profit."},
                          "scenarios_min_quantity": cases, "price_and_exit_basis_stress": stress})
    calendars = []
    for near, far in zip(dated, dated[1:]):
        near_ask = inverse_execution_price(raw["depth"][near["symbol"]]["asks"], 1)
        far_bid = inverse_execution_price(raw["depth"][far["symbol"]]["bids"], 1)
        calendars.append({"long": near["symbol"], "short": far["symbol"],
                          "entry_reciprocal_difference_btc_per_contract": float(near["contractSize"]) * (1 / near_ask - 1 / far_bid),
                          "locked_profit_btc": None,
                          "reason": "Different expiries do not force both exits to the same price. Near expiry leaves far exposure; closing or rolling changes PnL."})
    return {"report_version": 1, "generated_utc": datetime.fromtimestamp(now / 1000, timezone.utc).isoformat(),
            "read_only": True, "objective": "net BTC accumulation", "equity_btc": equity,
            "leverage_assumption": leverage, "unallocated_equity_fraction": reserve,
            "taker_and_delivery_fee_assumption": fee_rate, "index_usd": index,
            "fee_status": "Assumed 5bps per execution/settlement; account fee tier is not queried.",
            "current_funding_quote": {"api_field": "lastFundingRate", "rate": float(premium[perp["symbol"]]["lastFundingRate"]),
                                      "next_funding_time_ms": int(premium[perp["symbol"]]["nextFundingTime"]),
                                      "repeat_scenario_assumes_interval_hours": 8,
                                      "warning": "A current API quote is not a future funding guarantee."},
            "funding_history": funding, "perp_delivery_pairs": pairs, "dated_calendar_pairs": calendars,
            "decision": "RESEARCH_ONLY_NO_EXECUTION: scenario signs depend on unknown future funding and exit basis",
            "limitations": [
                "Entry quotes are sequential snapshots; both-leg atomic execution is not available.",
                "Perpetual can differ from dated settlement index; exit basis is not guaranteed to be zero.",
                "Positive funding pays shorts, so long-perpetual carry can lose money despite a positive futures premium.",
                "Future funding path and fee tier are unknown. Historical-rate scenarios are not forecasts.",
                "Daily funding extrapolation uses fractional events; actual funding dates/intervals can change, especially relevant near expiry.",
                "No maintenance bracket or account liquidation check; exchangeInfo maintMarginPercent is documented as ignored.",
                "3x isolated legs can liquidate separately despite offsetting aggregate exposure.",
                "Constant-price funding scenarios omit path variation; stress table shows terminal-price and exit-basis sensitivity.",
                "BTC spot plus an inverse short locks USD value and can reduce BTC units if BTC price rises.",
            ], "sources": SOURCES}


def scan(output: Path, equity: float = .007, leverage: float = 3,
         reserve: float = .25, fee_rate: float = .0005) -> dict:
    output = Path(output)
    for name in ("public_snapshot.json", "basis_report.json"):
        if (output / name).exists():
            raise FileExistsError(f"Research artifact already exists: {output / name}")
    positive(equity, "equity")
    positive(leverage, "leverage")
    if not 0 <= reserve < 1 or not 0 <= fee_rate < 1:
        raise ValueError("Invalid reserve or fee")
    exchange = public_get("/dapi/v1/exchangeInfo")
    now_start = public_get("/dapi/v1/time")["serverTime"]
    symbols = [s for s in exchange["symbols"] if s.get("baseAsset") == "BTC" and s.get("marginAsset") == "BTC"
               and s.get("contractStatus") == "TRADING" and (s["contractType"] == "PERPETUAL" or s["deliveryDate"] > now_start)]
    perp = next(s for s in symbols if s["contractType"] == "PERPETUAL")
    funding = []
    start = now_start - 90 * DAY_MS
    while start <= now_start:
        page = public_get("/dapi/v1/fundingRate", symbol=perp["symbol"], startTime=start, endTime=now_start, limit=1000)
        if not page:
            break
        funding.extend(page)
        next_start = max(int(r["fundingTime"]) for r in page) + 1
        if next_start <= start:
            raise ValueError("Funding pagination did not advance")
        start = next_start
        if len(page) < 1000:
            break
    premium = [r for r in public_get("/dapi/v1/premiumIndex") if r["symbol"] in {s["symbol"] for s in symbols}]
    depth = {s["symbol"]: public_get("/dapi/v1/depth", symbol=s["symbol"], limit=20) for s in symbols}
    raw = {"server_time": public_get("/dapi/v1/time"), "symbols": symbols,
           "premium": premium, "depth": depth, "funding": funding,
           "public_api_base": BASE, "retrieved_local_ms": int(time.time() * 1000)}
    report = build_report(raw, equity, leverage, reserve, fee_rate)
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects old artifacts if another process writes
    # between the pre-network check above and this save.
    for name, payload in (("public_snapshot.json", raw), ("basis_report.json", report)):
        with (output / name).open("x", encoding="utf-8") as artifact:
            json.dump(payload, artifact, ensure_ascii=False, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_output = Path("btc_lab/state") / datetime.now(timezone.utc).strftime("basis_%Y%m%dT%H%M%S_%fZ")
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--equity", type=float, default=.007)
    parser.add_argument("--leverage", type=float, default=3)
    parser.add_argument("--reserve", type=float, default=.25)
    parser.add_argument("--fee-rate", type=float, default=.0005)
    args = parser.parse_args()
    report = scan(args.output, args.equity, args.leverage, args.reserve, args.fee_rate)
    print(json.dumps({"generated_utc": report["generated_utc"], "output": str(args.output),
                      "decision": report["decision"], "pairs": [{"long": p["long"], "short": p["short"],
                          "days": round(p["days_to_delivery"], 3),
                          "net_btc_repeat_30d_rate": p["scenarios_min_quantity"]["repeat_last_30d_rate"]["net_btc"],
                          "capacity": p["capacity_pairs_with_reserve_not_recommendation"],
                          "fresh": p["quotes_fresh"]} for p in report["perp_delivery_pairs"]]}, indent=2))


if __name__ == "__main__":
    main()
