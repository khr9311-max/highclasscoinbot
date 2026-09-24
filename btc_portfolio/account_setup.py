"""Explicit, narrow BTCUSD_PERP margin/leverage setup before portfolio funding."""
import argparse
import asyncio
import json
from pathlib import Path

from btc_spot.config import DEFAULT_CREDENTIALS, credentials
from btc_spot.runtime import safe_error
from btc_spot.store import number

from .config import load
from .venues import Venues


SYMBOL = "BTCUSD_PERP"


def preview(account, leverage):
    position = account["position"]
    blockers = []
    if account["hedge_mode"]:
        blockers.append("HEDGE_MODE_ENABLED")
    if account["coin_orders"] or account["algos"]:
        blockers.append("COINM_OPEN_ORDERS")
    if any(number(p.position_amt) != 0 for p in account["coin"].positions):
        blockers.append("COINM_POSITION_PRESENT")
    if any(number(a.initial_margin) != 0 or number(a.open_order_initial_margin) != 0
           for a in account["coin"].assets.values()):
        blockers.append("COINM_MARGIN_IN_USE")
    if not account["coin"].can_trade:
        blockers.append("COINM_TRADING_DISABLED")
    if account["permissions"].get("enableFutures") is not True:
        blockers.append("API_FUTURES_PERMISSION_DISABLED")
    if position.symbol != SYMBOL or position.position_side != "BOTH":
        blockers.append("UNEXPECTED_COINM_POSITION_MODE")
    return {"symbol": SYMBOL, "margin_type_now": position.margin_type,
            "leverage_now": position.leverage, "margin_type_target": "isolated",
            "leverage_target": leverage, "ready": not blockers, "blockers": blockers,
            "changes_submitted": 0, "orders_submitted": 0}


def setup_guard(method, path, params, leverage):
    if method != "POST" or params.get("symbol") != SYMBOL:
        raise ValueError("Account setup endpoint or symbol rejected")
    if path == "/dapi/v1/marginType" and params == {"symbol": SYMBOL, "marginType": "ISOLATED"}:
        return
    if path == "/dapi/v1/leverage" and params == {"symbol": SYMBOL, "leverage": leverage}:
        return
    raise ValueError("Account setup parameters rejected")


async def dispatch(args):
    config = load(args.config)
    venues = Venues(config, credentials(args.credentials_file), allow_orders=False)
    try:
        account = await venues.account()
        plan = preview(account, config.max_leverage)
        if args.action == "prepare" or not plan["ready"]:
            return plan
        if args.confirm != "I_UNDERSTAND_COINM_ACCOUNT_SETUP":
            raise ValueError("Explicit COIN-M account setup confirmation required")
        await venues.rest.sync_time()
        venues.rest.mutation_guard = lambda method, path, params: setup_guard(
            method, path, params, config.max_leverage)
        changes = 0
        if account["position"].margin_type != "isolated":
            await venues.coin.set_margin_type(SYMBOL, "ISOLATED")
            changes += 1
            account = await venues.account()
            if account["position"].margin_type != "isolated" or not preview(account, config.max_leverage)["ready"]:
                raise ValueError("COIN-M margin setting was not confirmed")
        if account["position"].leverage != config.max_leverage:
            await venues.coin.set_leverage(SYMBOL, config.max_leverage)
            changes += 1
            account = await venues.account()
            if account["position"].leverage != config.max_leverage or not preview(account, config.max_leverage)["ready"]:
                raise ValueError("COIN-M leverage setting was not confirmed")
        result = preview(account, config.max_leverage)
        result["changes_submitted"] = changes
        result["configured"] = result["ready"] and result["margin_type_now"] == "isolated" and (
            result["leverage_now"] == config.max_leverage)
        return result
    finally:
        await venues.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "configure"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = asyncio.run(dispatch(args))
        print(json.dumps(result, indent=2))
        return 0 if result.get("ready") else 2
    except Exception as exc:
        print(json.dumps({"status": "ERROR", **safe_error(exc),
                          **({"reason": str(exc)} if type(exc) is ValueError else {})}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
