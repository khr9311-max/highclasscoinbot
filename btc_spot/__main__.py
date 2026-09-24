"""Spot preparation and execution CLI. Default mode is paper."""
import argparse
import asyncio
import json
from pathlib import Path

from .config import DEFAULT_CAPITAL, DEFAULT_CREDENTIALS, LIVE_CONFIRMATION, capital, credentials, state_directory
from .runtime import prepare, run_loop, safe_error


async def dispatch(args):
    from .gateway import SpotGateway
    directory = (args.state_dir or state_directory("live" if args.action == "prepare" else args.mode)).resolve()
    if args.action == "prepare" or args.mode == "live":
        if args.action == "run" and args.confirm != LIVE_CONFIRMATION:
            raise ValueError("Live start requires --confirm I_UNDERSTAND_LIVE_SPOT")
        secret = credentials(args.credentials_file)
        gateway = SpotGateway(secret.api_key, secret.api_secret,
                              allow_orders=args.action == "run" and args.mode == "live")
    else:
        gateway = SpotGateway()
    if args.action == "prepare":
        try:
            return await prepare(gateway, args.initial_btc, directory/"readiness.json")
        finally:
            await gateway.close()
    if args.mode == "paper":
        from .paper import PaperGateway
        gateway = PaperGateway(gateway, directory/"paper_exchange.json", initial_btc=args.initial_btc)
    return await run_loop(gateway, directory, args.mode, args.initial_btc,
                          once=args.once, poll_seconds=args.poll_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--initial-btc", type=capital, default=DEFAULT_CAPITAL)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 10 <= args.poll_seconds <= 300:
        parser.error("--poll-seconds must be 10..300")
    try:
        result = asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", **safe_error(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
    return 1 if result.get("status") == "ERROR" else 0


if __name__ == "__main__":
    raise SystemExit(main())
