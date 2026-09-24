"""Public-data forward PAPER observation, never exchange order execution.

Completed hourly bars are replayed after their close. Fills are modeled at their
opening prices; this is prospective paper observation, not actual fill evidence.
The first eligible target is the UTC daily opening after initialization.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from binance_coinm_v1.runtime.instance_lock import InstanceLock
from .engine import Bar, Config, Funding, Spec, run as simulate
from .growth_metrics import growth_metrics
from .research import Day, target_schedule

HOUR, DAY = 3600, 86400
SYMBOL = "BTCUSD_PERP"
CASES = {"momentum60_stop20": (.60, 2.0), "momentum40_stop20": (.40, 1.25)}
ALLOWLIST = {
    "/dapi/v1/time": set(), "/dapi/v1/exchangeInfo": set(),
    "/dapi/v1/klines": {"symbol", "interval", "startTime", "endTime", "limit"},
    "/dapi/v1/markPriceKlines": {"symbol", "interval", "startTime", "endTime", "limit"},
    "/dapi/v1/fundingRate": {"symbol", "startTime", "endTime", "limit"},
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Public API redirect refused")


class PublicClient:
    """Fixed Binance public host, GET only, no credentials or arbitrary URLs."""
    def __init__(self, timeout=10):
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def get(self, path, params=None):
        params = params or {}
        if path not in ALLOWLIST or not set(params) <= ALLOWLIST[path]:
            raise ValueError("Endpoint or parameter is outside the public GET allowlist")
        if "symbol" in params and params["symbol"] != SYMBOL:
            raise ValueError("Only BTCUSD_PERP public data is supported")
        if "interval" in params and params["interval"] not in {"1h", "1d"}:
            raise ValueError("Only 1h/1d public candles are supported")
        url = "https://dapi.binance.com" + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, method="GET", headers={"User-Agent": "btc-lab-forward-paper/1"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(10_000_001)
        except HTTPError as exc:
            raise RuntimeError(f"Public GET failed with HTTP {exc.code}; state preserved") from exc
        if len(raw) > 10_000_000:
            raise ValueError("Public response exceeds the allowed size")
        value = json.loads(raw)
        if isinstance(value, dict) and isinstance(value.get("code"), (int, float)) and value["code"] < 0:
            raise RuntimeError(f"Public API error code {value['code']}")
        return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _ohlc(values):
    prices = tuple(float(x) for x in values)
    if len(prices) != 4 or not all(math.isfinite(x) and x > 0 for x in prices):
        raise ValueError("Invalid public OHLC prices")
    o, h, l, c = prices
    if not l <= min(o, c) <= max(o, c) <= h:
        raise ValueError("Inconsistent public OHLC range")
    return prices


def _candles(client, path, interval, start, end):
    """Fetch exactly [start,end), with bounded windows and no open bars."""
    period = DAY if interval == "1d" else HOUR
    if start % period or end % period or end < start:
        raise ValueError("Candle window is not aligned")
    rows = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor + min(200 * DAY, 1000 * period))
        raw = client.get(path, {"symbol": SYMBOL, "interval": interval,
                              "startTime": cursor * 1000, "endTime": stop * 1000 - 1,
                              "limit": 1000})
        if not isinstance(raw, list):
            raise ValueError("Public candles must be an array")
        parsed = []
        for item in raw:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError("Malformed public candle")
            opened = int(item[0])
            if opened % (period * 1000) or int(item[6]) != opened + period * 1000 - 1:
                raise ValueError("Invalid candle boundaries")
            prices = _ohlc(item[1:5])
            parsed.append({"t": opened // 1000, **dict(zip("ohlc", prices))})
        if [row["t"] for row in parsed] != list(range(cursor, stop, period)):
            raise ValueError("Public candle window is incomplete, duplicated, reordered or still open")
        rows.extend(parsed)
        cursor = stop
    return rows


def _funding(client, start, end):
    rows = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor + 30 * DAY)
        raw = client.get("/dapi/v1/fundingRate", {"symbol": SYMBOL, "startTime": cursor * 1000,
                         "endTime": stop * 1000 - 1, "limit": 1000})
        if not isinstance(raw, list) or len(raw) >= 1000:
            raise ValueError("Funding window is malformed or truncated")
        for item in raw:
            if item.get("symbol") != SYMBOL:
                raise ValueError("Unexpected funding symbol")
            timestamp = int(item["fundingTime"]) / 1000
            rate = float(item["fundingRate"])
            mark = float(item["markPrice"]) if item.get("markPrice") not in (None, "") else None
            if (not cursor <= timestamp < stop or not math.isfinite(rate)
                    or (mark is not None and (not math.isfinite(mark) or mark <= 0))):
                raise ValueError("Invalid funding data")
            rows.append({"t": timestamp, "rate": rate, "mark": mark})
        cursor = stop
    times = [row["t"] for row in rows]
    if times != sorted(set(times)):
        raise ValueError("Funding history is duplicated or out of order")
    return rows


def _spec(client, maintenance):
    payload = client.get("/dapi/v1/exchangeInfo")
    choices = [x for x in payload.get("symbols", []) if x.get("symbol") == SYMBOL]
    if len(choices) != 1:
        raise ValueError("BTCUSD_PERP contract metadata missing")
    raw = choices[0]
    if (raw.get("contractStatus") != "TRADING" or raw.get("contractType") != "PERPETUAL"
            or raw.get("marginAsset") != "BTC" or raw.get("quoteAsset") != "USD"):
        raise ValueError("Unexpected BTCUSD_PERP contract state")
    filters = {x["filterType"]: x for x in raw["filters"]}
    lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
    spec = Spec(float(raw["contractSize"]), float(lot["stepSize"]), float(lot["minQty"]),
                float(filters["PRICE_FILTER"]["tickSize"]), maintenance)
    if not all(math.isfinite(x) and x > 0 for x in asdict(spec).values()):
        raise ValueError("Invalid public contract specifications")
    return asdict(spec)


def _merge(old, new, overlap_start, previous_end=None):
    """Append-only inputs; late revisions are errors, never silent restatements."""
    old_by_time = {row["t"]: row for row in old}
    new_by_time = {row["t"]: row for row in new}
    for timestamp, row in old_by_time.items():
        if timestamp >= overlap_start and new_by_time.get(timestamp) != row:
            raise ValueError("Previously observed public data was revised or removed")
    for timestamp, row in new_by_time.items():
        if timestamp in old_by_time and old_by_time[timestamp] != row:
            raise ValueError("Previously observed public data was revised")
        if timestamp not in old_by_time and previous_end is not None and timestamp < previous_end:
            raise ValueError("Late historical funding would alter the observed ledger")
        old_by_time[timestamp] = row
    return [old_by_time[t] for t in sorted(old_by_time)]


def _observation(result, spec):
    """Remove only the engine's terminal artificial liquidation from observation."""
    value = copy.deepcopy(result)
    ledger, curve = value["events"], value["equity_curve"]
    cash, position, entry = curve[-1]["cash_btc"], 0.0, None
    if ledger and ledger[-1]["type"] == "fill" and ledger[-1]["reason"] == "end_of_data":
        terminal = ledger.pop()
        position = terminal["position_before"]
        cash = terminal["cash_btc"] - terminal["realized_btc"] + terminal["fee_btc"] + terminal["liquidation_fee_btc"]
        entry = 1 / (terminal["realized_btc"] / (position * spec.contract_size) + 1 / terminal["price"])
        equity = cash + position * spec.contract_size * (1 / entry - 1 / curve[-1]["mark_price"])
        curve[-1].update(cash_btc=cash, position=position, equity_btc=equity,
                        exposure=position * spec.contract_size / curve[-1]["mark_price"] / equity if equity > 0 else 0)
        value["trades"].pop()
    equity = curve[-1]["equity_btc"]
    regular_fees = sum(row.get("fee_btc", 0) for row in ledger)
    realized = sum(row.get("realized_btc", 0) for row in ledger)
    funding = sum(row.get("funding_btc", 0) for row in ledger)
    liquidation_fees = sum(row.get("liquidation_fee_btc", 0) for row in ledger)
    initial = value["summary"]["initial_btc"]
    if not math.isclose(cash, initial + realized + funding - regular_fees - liquidation_fees,
                        rel_tol=1e-10, abs_tol=1e-12):
        raise AssertionError("Open observation wallet does not reconcile")
    return {"status": "PAPER_BAR_CLOSE_REPLAY", "orders_enabled": False,
            "actual_fill_evidence": False, "cash_btc": cash, "equity_btc": equity,
            "position_contracts": position, "average_entry": entry,
            "net_btc": equity - initial, "return_pct": (equity / initial - 1) * 100,
            "fee_btc": regular_fees, "funding_btc": funding, "realized_btc": realized,
            "liquidation_fee_btc": liquidation_fees, "closed_trades": value["trades"],
            "ledger": ledger, "equity_curve": curve, "growth": growth_metrics(value),
            "engine_audit": value["audit"], "as_of": curve[-1]["t"]}


class ForwardRunner:
    def __init__(self, state_dir, equity, fee, candidate="momentum60_stop20", *,
                 maint_margin_rate=.01, warmup_days=350):
        if candidate not in CASES:
            raise ValueError("Unknown paper candidate")
        if not math.isfinite(equity) or equity <= 0 or not math.isfinite(fee) or not 0 <= fee < .1:
            raise ValueError("Valid BTC equity and taker fee are required")
        if not math.isfinite(maint_margin_rate) or not 0 < maint_margin_rate < .5:
            raise ValueError("Invalid maintenance margin rate")
        if not isinstance(warmup_days, int) or not 201 <= warmup_days <= 350:
            raise ValueError("Warmup must contain between 201 and 350 completed days")
        self.directory = Path(state_dir).resolve()
        self.path = self.directory / "state.json"
        self.lock = InstanceLock(self.directory / "instance.lock")
        sources = [Path(__file__), Path(__file__).with_name("engine.py"),
                   Path(__file__).with_name("research.py"), Path(__file__).with_name("growth_metrics.py")]
        self.manifest = {"version": 1, "symbol": SYMBOL, "candidate": candidate,
            "initial_btc": float(equity), "taker_fee": float(fee), "warmup_days": warmup_days,
            "maintenance_margin_rate": float(maint_margin_rate), "slippage_bps": 3,
            "stop_slippage_bps": 10, "stop_pct": .20, "leverage": 3,
            "intrabar_funding_policy": "adverse", "close_settlement_delay_sec": 30,
            "orders_enabled": False, "mode": "PAPER_BAR_CLOSE_REPLAY",
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
        self._owned = False

    def __enter__(self):
        self.lock.acquire()
        self._owned = True
        return self

    def __exit__(self, *args):
        self._owned = False
        self.lock.release()

    def _load(self):
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text(encoding="utf-8"))
        supplied = state.pop("integrity_sha256", None)
        if supplied != _digest(state):
            raise ValueError("Paper state integrity check failed; state preserved")
        if state.get("manifest") != self.manifest:
            raise ValueError("Paper parameters or source changed; choose a new state directory")
        return state

    def refresh(self, client=None):
        if not self._owned:
            raise RuntimeError("Acquire the OS instance lock before refreshing paper state")
        client = client or PublicClient()
        state = self._load()
        raw_clock = client.get("/dapi/v1/time")
        server_ms = int(raw_clock["serverTime"])
        if server_ms <= 0:
            raise ValueError("Invalid server clock")
        now = server_ms // 1000
        closed_end = (now - 30) // HOUR * HOUR
        closed_day_end = (now - 30) // DAY * DAY
        previous = copy.deepcopy(state)
        if state is None:
            state = {"manifest": self.manifest, "created_server_ms": server_ms,
                     "activation_time": (now // DAY + 1) * DAY,
                     "spec": _spec(client, self.manifest["maintenance_margin_rate"]),
                     "days": _candles(client, "/dapi/v1/klines", "1d",
                         closed_day_end - self.manifest["warmup_days"] * DAY, closed_day_end),
                     "bars": [], "funding": [], "observation": None,
                     "last_spec_check": closed_day_end}
        elif server_ms < state["last_refresh_server_ms"]:
            raise ValueError("Server clock moved backwards; state preserved")

        if closed_day_end > state["last_spec_check"]:
            if _spec(client, self.manifest["maintenance_margin_rate"]) != state["spec"]:
                raise ValueError("Public contract specifications changed; state preserved")
            state["last_spec_check"] = closed_day_end
        if state["days"][-1]["t"] + DAY < closed_day_end:
            daily_start = state["days"][-1]["t"]
            added_days = _candles(client, "/dapi/v1/klines", "1d", daily_start, closed_day_end)
            state["days"] = _merge(state["days"], added_days, daily_start)

        start = state["activation_time"]
        old_end = state["bars"][-1]["t"] + HOUR if state["bars"] else start
        if closed_end > old_end:
            hour_start = max(start, old_end - 2 * HOUR)
            contracts = _candles(client, "/dapi/v1/klines", "1h", hour_start, closed_end)
            marks = _candles(client, "/dapi/v1/markPriceKlines", "1h", hour_start, closed_end)
            additions = [{**contract, **{"mark_" + k: mark[k] for k in "ohlc"}}
                         for contract, mark in zip(contracts, marks)]
            state["bars"] = _merge(state["bars"], additions, hour_start)
            funding_start = max(start, old_end - DAY)
            added_funding = _funding(client, funding_start, closed_end)
            state["funding"] = _merge(state["funding"], added_funding, funding_start, old_end)
            volatility, cap = CASES[self.manifest["candidate"]]
            targets = target_schedule([Day(**day) for day in state["days"]],
                                      "momentum_20_60_120", volatility, cap)
            bar_times = {b["t"] for b in state["bars"]}
            targets = {t: value for t, value in targets.items() if t >= start and t in bar_times}
            spec = Spec(**state["spec"])
            cfg = Config(initial_btc=self.manifest["initial_btc"], fee=self.manifest["taker_fee"],
                         max_exposure=cap, stop_pct=.20, intrabar_funding_policy="adverse")
            replay = simulate([Bar(**b) for b in state["bars"]], [Funding(**f) for f in state["funding"]], targets, spec, cfg)
            observation = _observation(replay, spec)
            if previous and previous["observation"]:
                old_ledger = previous["observation"]["ledger"]
                if observation["ledger"][:len(old_ledger)] != old_ledger:
                    raise ValueError("Replay changed the previously observed ledger; state preserved")
            state["observation"] = observation
            state["targets"] = {str(t): target for t, target in targets.items()}
        state["last_refresh_server_ms"] = server_ms
        state["last_completed_end"] = closed_end
        state["limitations"] = [
            "PAPER only. No exchange orders or authenticated calls exist in this runner.",
            "Completed-hour replay is not real-time execution, latency or fill-quality evidence.",
            "Signals start only at the UTC daily opening after first initialization; warmup has no trades.",
            "Shared BTC collateral model, constant maintenance rate and assumed slippage/liquidation fees.",
            "Only completed contract and mark candles are accepted; hourly internal paths remain unknown.",
            "Funding may arrive late; a revision stops refresh rather than changing prior paper performance.",
            "The artificial final backtest close is excluded; open paper positions carry into the next refresh.",
        ]
        state["integrity_sha256"] = _digest(state)
        _atomic_json(self.path, state)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--equity", type=float, required=True, help="Initial PAPER BTC balance")
    parser.add_argument("--fee", type=float, required=True, help="Taker fee as a decimal fraction")
    parser.add_argument("--candidate", choices=CASES, default="momentum60_stop20")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--maint-margin-rate", type=float, default=.01)
    parser.add_argument("--warmup-days", type=int, default=350)
    parser.add_argument("--duration", type=float, default=0, help="Seconds to run; 0 means until interrupted")
    parser.add_argument("--poll-sec", type=float, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop-file", type=Path, help="Exit normally when this local request file exists")
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration < 0 or not math.isfinite(args.poll_sec) or args.poll_sec < 10:
        parser.error("Duration must be finite/nonnegative and poll interval at least 10 seconds")
    stop = threading.Event()
    def request_stop(signum, frame):
        stop.set()
    old_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    began = time.monotonic()
    failures = 0
    try:
        with ForwardRunner(args.state_dir, args.equity, args.fee, args.candidate,
                           maint_margin_rate=args.maint_margin_rate, warmup_days=args.warmup_days) as runner:
            while not stop.is_set():
                if args.stop_file is not None and args.stop_file.exists():
                    break
                try:
                    state = runner.refresh()
                    observation = state["observation"] or {}
                    print(json.dumps({"mode": "PAPER_BAR_CLOSE_REPLAY", "orders_enabled": False,
                          "server_ms": state["last_refresh_server_ms"], "activation_time": state["activation_time"],
                          "observed_hours": len(state["bars"]), "equity_btc": observation.get("equity_btc", args.equity),
                          "position_contracts": observation.get("position_contracts", 0),
                          "ledger_events": len(observation.get("ledger", []))}), flush=True)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    print(json.dumps({"mode": "PAPER", "status": "refresh_failed_state_preserved",
                                      "error": f"{type(exc).__name__}: {exc}"}), flush=True)
                    if args.once or failures >= 3:
                        return 1
                remaining = args.duration - (time.monotonic() - began) if args.duration else None
                if args.once or (remaining is not None and remaining <= 0):
                    break
                wait_until = time.monotonic() + (min(args.poll_sec, remaining) if remaining is not None else args.poll_sec)
                while not stop.is_set() and time.monotonic() < wait_until:
                    if args.stop_file is not None and args.stop_file.exists():
                        stop.set()
                        break
                    stop.wait(min(1, max(0, wait_until - time.monotonic())))
        return 0
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
