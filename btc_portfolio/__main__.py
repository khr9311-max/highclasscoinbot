import argparse
import asyncio
import json
from pathlib import Path

from btc_spot.config import credentials, DEFAULT_CREDENTIALS
from btc_spot.runtime import safe_error
from .config import load, ROOT
from .runtime import prepare, run
from .signals import alt_signal, coinm_signal
from .venues import Venues


async def dispatch(args):
    config = load(args.config)
    if args.action == "run" and args.confirm != "I_UNDERSTAND_LIVE_PORTFOLIO":
        raise ValueError("Use --confirm I_UNDERSTAND_LIVE_PORTFOLIO for real order submission")
    venues = Venues(config, credentials(args.credentials_file) if args.action != "observe" else None,
                    allow_orders=args.action == "run")
    try:
        if args.action == "observe":
            markets = await venues.markets()
            return {"orders_submitted": 0, "alt": alt_signal(markets["spot"]), "coinm": coinm_signal(markets["coinm"])}
        if args.action == "prepare":
            return await prepare(venues, config, args.state_dir)
        return await run(venues, config, args.state_dir, once=args.once, poll_seconds=args.poll_seconds)
    finally:
        await venues.close()


def main():
    parser = argparse.ArgumentParser(description="BTC portfolio: alt/BTC spot + BTC-settled COIN-M long/short")
    parser.add_argument("action", choices=("observe", "prepare", "run"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--state-dir", type=Path, default=ROOT/"btc_portfolio/state/live")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 10 <= args.poll_seconds <= 300:
        parser.error("poll-seconds must be 10..300")
    try:
        result = asyncio.run(dispatch(args))
        print(json.dumps(result, default=str, indent=2))
        return 0 if result is None or result.get("ready", True) else 2
    except Exception as exc:
        print(json.dumps({"status": "ERROR", **safe_error(exc),
                          **({"reason": str(exc)} if type(exc) is ValueError else {})}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
