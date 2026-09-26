"""Hourly historical replay of the btc_portfolio execution rules at 0.003 BTC.

Mirrors btc_portfolio/engine.py: completed-day alt rotation (ETH/BNB/SOL/XRP
priced in BTC), one alt entry per signal day, no same-day re-entry after an
exit, a 5% alt stop polled by the bot, COIN-M EMA20/80 on completed 4h bars,
one entry attempt per 4h bar, reduce-only exit on reversal, an exchange-held
mark-price stop, inverse risk sizing in whole contracts, the 2% daily
new-entry gate and funding. All balances are BTC; holding BTC returns 0%.

Assumptions: IOC orders fill completely at the hourly open with the limit's
slippage; the separate spot-tick sensitivity rounds BUY up and SELL down.
Alt stops fill at the stop level (the bot polls every 30 seconds),
and today's spot filters and COIN-M contract rules apply to the whole history.
No credentials are used. `download` performs public GET requests only.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / "btc_lab/state/portfolio_market"
COINM_CACHE = ROOT / "binance_coinm_v1/state/cache"
BTCUSDT_DAILY = ROOT / "btc_lab/state/small_capital_market/BTCUSDT_1d_spot.csv"
OUTPUT = ROOT / "btc_lab/state/portfolio_backtest_20260925"
HOUR, H4, DAY = 3_600_000, 14_400_000, 86_400_000
ALTS = ("ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC")
DOWNLOAD_START = 1596240000000   # 2020-08-01 UTC
DOWNLOAD_END = 1790294400000     # 2026-09-25 UTC
START = "2020-10-01"             # first day with 61 completed alt days
END = "2026-09-24T09:00:00"      # last cached COIN-M hour + 1
CAPITAL = 0.003


def stamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def floor_step(value, step):
    return math.floor(value / step + 1e-9) * step


def read_ohlc(path):
    with open(path, newline="") as handle:
        rows = csv.reader(handle)
        next(rows)
        return {int(r[0]): tuple(float(x) for x in r[1:5]) for r in rows}


# ---------------------------------------------------------------- data

def public_get(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in {"api.binance.com", "dapi.binance.com"}:
        raise ValueError("Only public Binance market hosts are allowed")
    if parts.path not in {"/api/v3/klines", "/api/v3/exchangeInfo", "/dapi/v1/exchangeInfo"}:
        raise ValueError("Only public candle and filter endpoints are allowed")
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers={"User-Agent": "btc-lab-portfolio/1"}), timeout=20) as response:
                return json.loads(response.read(4_000_001))
        except OSError:
            time.sleep(1 + attempt)
    raise RuntimeError("Public request failed: " + parts.path)


def download(output=MARKET):
    output.mkdir(parents=True, exist_ok=True)
    for symbol in ALTS:
        for interval, period in (("1h", HOUR), ("1d", DAY)):
            rows, start = [], DOWNLOAD_START
            while start < DOWNLOAD_END:
                chunk = public_get("https://api.binance.com/api/v3/klines?" + urlencode(
                    {"symbol": symbol, "interval": interval, "startTime": start,
                     "endTime": DOWNLOAD_END - 1, "limit": 1000}))
                if not chunk:
                    break
                rows += [r for r in chunk if int(r[6]) < DOWNLOAD_END]
                start = int(chunk[-1][0]) + period
            with open(output / f"{symbol}_{interval}.csv", "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms"])
                writer.writerows(r[:7] for r in rows)
            print(symbol, interval, len(rows), flush=True)
    info = public_get("https://api.binance.com/api/v3/exchangeInfo?" + urlencode(
        {"symbols": json.dumps(list(ALTS), separators=(",", ":"))}))
    (output / "spot_filters.json").write_text(
        json.dumps({s["symbol"]: s["filters"] for s in info["symbols"]}, indent=1))
    coin = public_get("https://dapi.binance.com/dapi/v1/exchangeInfo")
    spec = next(s for s in coin["symbols"] if s["symbol"] == "BTCUSD_PERP")
    (output / "coinm_spec.json").write_text(json.dumps(spec, indent=1))


class Data:
    def __init__(self, market=MARKET, coinm=COINM_CACHE, alts=ALTS):
        self.alts = tuple(alts)
        self.alt_h = {s: read_ohlc(market / f"{s}_1h.csv") for s in self.alts}
        self.alt_d = {s: read_ohlc(market / f"{s}_1d.csv") for s in self.alts}
        self.c1h = read_ohlc(coinm / "BTCUSD_PERP_1h_contract.csv")
        self.m1h = read_ohlc(coinm / "BTCUSD_PERP_1h_mark.csv")
        self.c4h = read_ohlc(coinm / "BTCUSD_PERP_4h_contract.csv")
        self.funding = {}
        with open(coinm / "BTCUSD_PERP_funding.csv", newline="") as handle:
            rows = csv.reader(handle)
            next(rows)
            for r in rows:
                self.funding[int(r[0]) // HOUR * HOUR] = float(r[1])
        filters = json.loads((market / "spot_filters.json").read_text())
        pick = lambda s, kind: next(f for f in filters[s] if f["filterType"] == kind)
        self.step = {s: float(pick(s, "LOT_SIZE")["stepSize"]) for s in self.alts}
        self.tick = {s: float(pick(s, "PRICE_FILTER")["tickSize"]) for s in self.alts}
        self.min_notional = {s: float(pick(s, "NOTIONAL")["minNotional"]) for s in self.alts}
        self._coin, self._alt = {}, {}

    def coin_signal(self, t):
        """btc_portfolio.signals.coinm_signal on the 200 bars completed before t."""
        end = t // H4 * H4
        if end not in self._coin:
            opens = [end - H4 * k for k in range(200, 0, -1)]
            if any(o not in self.c4h for o in opens):
                self._coin[end] = None
                return None
            bars = [self.c4h[o] for o in opens]
            closes = [b[3] for b in bars]

            def ema(span):
                alpha, value = 2 / (span + 1), closes[0]
                for price in closes[1:]:
                    value += alpha * (price - value)
                return value
            fast, slow = ema(20), ema(80)
            direction = 1 if fast > slow else -1 if fast < slow else 0
            ranges = [max(bars[i][1] - bars[i][2], abs(bars[i][1] - closes[i - 1]), abs(bars[i][2] - closes[i - 1]))
                      for i in range(len(bars) - 14, len(bars))]
            stop = max(0.01, min(0.08, sum(ranges) / 14 * 2 / closes[-1]))
            self._coin[end] = (end - H4, direction, stop)
        return self._coin[end]

    def alt_signal(self, t):
        """btc_portfolio.signals.alt_signal on the 61 days completed before t."""
        end = t // DAY * DAY
        if end not in self._alt:
            scores = {}
            for symbol in self.alts:
                opens = [end - DAY * k for k in range(61, 0, -1)]
                if any(o not in self.alt_d[symbol] for o in opens):
                    self._alt[end] = None
                    return None
                c = [self.alt_d[symbol][o][3] for o in opens]
                scores[symbol] = (c[-1] / c[-21] - 1 + c[-1] / c[-61] - 1) / 2
            winner = max(sorted(scores), key=scores.get)
            self._alt[end] = (end - DAY, winner if scores[winner] > 0 else None, scores)
        return self._alt[end]


# ---------------------------------------------------------------- replay

class Portfolio:
    def __init__(self, data, *, spot_btc=0.0012, coinm_btc=0.0018, risk=0.005, daily_loss=0.02,
                 alt_stop=0.05, alt_max=0.25, coin_exposure=1.25, spot_fee=0.001, coin_fee=0.0005,
                 slip=0.0003, stop_slip=0.0005, cost_mult=1.0, fractional=False,
                 enable_alt=True, enable_coin=True, spot_tick_rounding=False):
        self.d = data
        self.spot_btc, self.coinm_btc, self.total = spot_btc, coinm_btc, spot_btc + coinm_btc
        self.risk, self.daily_loss = risk, daily_loss
        self.alt_stop, self.alt_max, self.coin_exposure = alt_stop, alt_max, coin_exposure
        self.spot_fee, self.coin_fee = spot_fee * cost_mult, coin_fee * cost_mult
        self.slip, self.stop_slip = slip * cost_mult, stop_slip * cost_mult
        self.fractional, self.enable_alt, self.enable_coin = fractional, enable_alt, enable_coin
        self.spot_tick_rounding = spot_tick_rounding
        self.wallet = {"BTC": spot_btc, **{s[:-3]: 0.0 for s in data.alts}}
        self.alt_entry = self.alt_day = None
        self.coin_wallet, self.coin_qty = coinm_btc, 0.0
        self.coin_entry = self.coin_stop = self.coin_bar = self.day_start = None
        self.trades, self.curve = [], []
        self.stats = {"alt_entries": 0, "alt_stops": 0, "alt_rotations": 0, "alt_skips": 0,
                      "coin_entries": 0, "coin_stops": 0, "coin_reversals": 0, "coin_min_contract_skips": 0,
                      "funding_btc": 0.0, "fees_btc": 0.0, "entry_blocked_days": 0}
        self._blocked_day = None

    def equity(self, t):
        value = self.wallet["BTC"] + self.coin_wallet
        for s in self.d.alts:
            if self.wallet[s[:-3]]:
                value += self.wallet[s[:-3]] * self.d.alt_h[s][t][0]
        if self.coin_qty:
            value += self.coin_pnl(self.d.m1h[t][0])
        return value

    def coin_pnl(self, price):
        side = 1 if self.coin_qty > 0 else -1
        return abs(self.coin_qty) * 100 * (1 / self.coin_entry - 1 / price) * side

    def sellable(self, s, t):
        qty = floor_step(self.wallet[s[:-3]], self.d.step[s])
        return qty > 0 and qty * self.d.alt_h[s][t][0] * (1 - self.slip) >= self.d.min_notional[s]

    def alt_sell(self, s, t, price, reason):
        if self.spot_tick_rounding:
            price = floor_step(price, self.d.tick[s])
        qty = floor_step(self.wallet[s[:-3]], self.d.step[s])
        fee = qty * price * self.spot_fee
        self.wallet[s[:-3]] -= qty
        self.wallet["BTC"] += qty * price - fee
        self.stats["fees_btc"] += fee
        entry = self.alt_entry[1] if self.alt_entry and self.alt_entry[0] == s else None
        pnl = qty * (price - entry) - fee if entry else None
        self.trades.append({"venue": "spot", "symbol": s, "reason": reason, "time": date(t),
                            "entry": entry, "exit": price, "quantity": qty, "pnl_btc": pnl})
        self.stats["alt_stops" if reason == "stop" else "alt_rotations"] += 1
        self.alt_entry = None

    def alt_buy(self, s, t, equity):
        price = self.d.alt_h[s][t][0] * (1 + self.slip)
        if self.spot_tick_rounding:
            price = math.ceil(price / self.d.tick[s] - 1e-9) * self.d.tick[s]
        allocation = min(self.spot_btc * self.alt_max,
                         min(equity, self.total) * self.risk / (self.alt_stop + 2 * self.spot_fee + 0.001),
                         self.wallet["BTC"] / (1 + self.spot_fee))
        qty = floor_step(allocation / price, self.d.step[s])
        if qty <= 0 or qty * price < self.d.min_notional[s]:
            self.stats["alt_skips"] += 1
            return
        self.wallet["BTC"] -= qty * price
        self.wallet[s[:-3]] += qty * (1 - self.spot_fee)  # buy commission in the received asset
        self.stats["fees_btc"] += qty * self.spot_fee * price
        self.alt_entry = (s, price)
        self.stats["alt_entries"] += 1

    def coin_close(self, t, price, reason):
        fee = abs(self.coin_qty) * 100 / price * self.coin_fee
        pnl = self.coin_pnl(price) - fee
        self.coin_wallet += pnl
        self.stats["fees_btc"] += fee
        self.trades.append({"venue": "coinm", "symbol": "BTCUSD_PERP", "reason": reason, "time": date(t),
                            "entry": self.coin_entry, "exit": price, "quantity": self.coin_qty, "pnl_btc": pnl})
        self.stats["coin_stops" if reason == "stop" else "coin_reversals"] += 1
        self.coin_qty, self.coin_entry, self.coin_stop = 0.0, None, None

    def coin_enter(self, t, direction, stop_fraction, equity):
        """coin_plan: taker IOC at +-3 bps, mark stop, inverse loss per contract."""
        if direction == 0:
            return
        opening, mark = self.d.c1h[t][0], self.d.m1h[t][0]
        raw = opening * (1 + direction * self.slip)
        entry = math.ceil(raw * 10) / 10 if direction > 0 else math.floor(raw * 10) / 10
        raw_stop = mark * (1 - direction * stop_fraction)
        stop = math.floor(raw_stop * 10) / 10 if direction > 0 else math.ceil(raw_stop * 10) / 10
        exit_price = stop * (1 - direction * 0.0005)
        loss = 100 * abs(1 / entry - 1 / exit_price) + 100 * self.coin_fee * (1 / entry + 1 / exit_price)
        available = self.coin_wallet
        limit = min(min(self.total, equity) * self.risk / loss,
                    min(self.coinm_btc, available) * entry / 100 * self.coin_exposure,
                    available / (100 / entry * (1 + self.coin_fee)))
        qty = limit if self.fractional else math.floor(limit + 1e-12)
        if qty <= 0 or (not self.fractional and qty < 1):
            self.stats["coin_min_contract_skips"] += 1
            return
        fee = qty * 100 / entry * self.coin_fee
        self.coin_wallet -= fee
        self.stats["fees_btc"] += fee
        self.coin_qty, self.coin_entry, self.coin_stop = qty * direction, entry, stop
        self.stats["coin_entries"] += 1

    def decide(self, t):
        """One Engine.tick decision pass. Returns True if it changed a position."""
        equity = self.equity(t)
        can_enter = equity > self.day_start[1] * (1 - self.daily_loss)
        if not can_enter and self._blocked_day != self.day_start[0]:
            self._blocked_day = self.day_start[0]
            self.stats["entry_blocked_days"] += 1
        alt = self.d.alt_signal(t) if self.enable_alt else None
        coin = self.d.coin_signal(t) if self.enable_coin else None
        if alt:
            for s in self.d.alts:
                if self.sellable(s, t):
                    bid = self.d.alt_h[s][t][0]
                    stopped = (self.alt_entry and self.alt_entry[0] == s
                               and bid <= self.alt_entry[1] * (1 - self.alt_stop))
                    if stopped or alt[1] != s:
                        self.alt_sell(s, t, bid * (1 - self.slip), "stop" if stopped else "rotate")
                        self.alt_day = alt[0]
                        return True
        if coin and self.coin_qty and (1 if self.coin_qty > 0 else -1) != coin[1]:
            side = 1 if self.coin_qty > 0 else -1
            self.coin_close(t, self.d.c1h[t][0] * (1 - side * self.slip), "reverse")
            return True
        if coin and can_enter and not self.coin_qty and self.coin_bar != coin[0]:
            self.coin_bar = coin[0]
            self.coin_enter(t, coin[1], coin[2], equity)
            if self.coin_qty:
                return True
        holding = any(self.sellable(s, t) for s in self.d.alts)
        if alt and can_enter and not holding and alt[1] and self.alt_day != alt[0]:
            self.alt_day = alt[0]
            before = self.stats["alt_entries"]
            self.alt_buy(alt[1], t, equity)
            return self.stats["alt_entries"] != before
        return False

    def hour(self, t):
        if self.coin_qty and t in self.d.funding:
            payment = self.coin_qty * 100 / self.d.m1h[t][0] * self.d.funding[t]
            self.coin_wallet -= payment
            self.stats["funding_btc"] -= payment
        if not self.day_start or self.day_start[0] != t // DAY:
            self.day_start = (t // DAY, self.equity(t))
            self.curve.append((t, self.day_start[1]))
        # The bot polls every 30 seconds; follow-up polls in the same hour act on the same prices.
        for _ in range(4):
            if not self.decide(t):
                break
        if self.coin_qty:
            _, high, low, _ = self.d.m1h[t]
            opening = self.d.c1h[t][0]
            if self.coin_qty > 0 and low <= self.coin_stop:
                self.coin_close(t, min(self.coin_stop, opening) * (1 - self.stop_slip), "stop")
            elif self.coin_qty < 0 and high >= self.coin_stop:
                self.coin_close(t, max(self.coin_stop, opening) * (1 + self.stop_slip), "stop")
        if self.alt_entry and self.sellable(self.alt_entry[0], t):
            symbol, entry = self.alt_entry
            opening, _, low, _ = self.d.alt_h[symbol][t]
            level = entry * (1 - self.alt_stop)
            if low <= level:
                self.alt_sell(symbol, t, min(level, opening) * (1 - self.slip), "stop")
                signal = self.d.alt_signal(t)
                self.alt_day = signal[0] if signal else self.alt_day

    def run(self, start, end):
        for t in range(start, end, HOUR):
            if t in self.d.c1h and t in self.d.m1h and all(t in self.d.alt_h[s] for s in self.d.alts):
                self.hour(t)
        return self


def spot_momentum(start, end, cost_mult=1.0, capital=CAPITAL, path=BTCUSDT_DAILY):
    """Approximation of btc_spot: 20/60/120-day votes, traded at the next day open."""
    rows = read_ohlc(path)
    days = sorted(rows)
    btc, usdt, curve = capital, 0.0, []
    fee, slip = 0.001 * cost_mult, 0.0003 * cost_mult
    for i, t in enumerate(days):
        if t < start or t >= end or i < 121:
            continue
        latest = rows[days[i - 1]][3]
        votes = sum((latest > rows[days[i - 1 - k]][3]) - (latest < rows[days[i - 1 - k]][3]) for k in (20, 60, 120))
        price = rows[t][0]
        delta = (3 + votes) / 6 * (btc * price + usdt) / price - btc
        if abs(delta) * price >= 5:
            if delta > 0:
                spend = min(usdt, delta * price * (1 + slip))
                btc += spend / (price * (1 + slip)) * (1 - fee)
                usdt -= spend
            else:
                sold = min(btc, -delta)
                btc -= sold
                usdt += sold * price * (1 - slip) * (1 - fee)
        curve.append((t, btc + usdt / rows[t][3]))
    return curve


# ---------------------------------------------------------------- metrics

def metrics(curve, start_value):
    values = [v for _, v in curve]
    peak = drawdown = 0.0
    since, underwater = curve[0][0], 0.0
    for t, v in curve:
        if v >= peak:
            peak, since = v, t
        drawdown = max(drawdown, 1 - v / peak)
        underwater = max(underwater, (t - since) / DAY)
    years = (curve[-1][0] - curve[0][0]) / DAY / 365.25
    growth = values[-1] / start_value
    worst = min((b / a - 1 for a, b in zip(values, values[365:])), default=None)
    return {"return": growth - 1, "annualized": growth ** (1 / years) - 1 if years > 0 else None,
            "max_drawdown": drawdown, "worst_365d": worst, "longest_underwater_days": underwater,
            "final_btc": values[-1]}


def study(data, output=OUTPUT):
    start, end = stamp(START), stamp(END)
    run = lambda s=start, e=end, **kw: Portfolio(data, **kw).run(s, e)
    result = {"period": [START, END], "capital_btc": CAPITAL, "assumptions": __doc__.split("\n\n")[1:3]}
    base = run()
    result["main"] = {
        "portfolio": {**metrics(base.curve, CAPITAL), **base.stats},
        "portfolio_spot_tick_rounded": metrics(run(spot_tick_rounding=True).curve, CAPITAL),
        "portfolio_cost_x2": metrics(run(cost_mult=2).curve, CAPITAL),
        "btc_spot_momentum": metrics(spot_momentum(start, end), CAPITAL),
        "btc_spot_momentum_cost_x2": metrics(spot_momentum(start, end, 2), CAPITAL)}
    late = stamp("2021-04-01")
    result["from_2021_04"] = {"portfolio": metrics(run(late).curve, CAPITAL),
                              "portfolio_cost_x2": metrics(run(late, cost_mult=2).curve, CAPITAL),
                              "btc_spot_momentum": metrics(spot_momentum(late, end), CAPITAL)}
    parts = {"alt_only": dict(enable_coin=False), "coinm_only": dict(enable_alt=False),
             "coinm_only_fractional_diagnostic": dict(enable_alt=False, fractional=True)}
    result["components"] = {}
    for name, options in parts.items():
        replay = run(**options)
        result["components"][name] = {**metrics(replay.curve, CAPITAL), **replay.stats}
    result["calendar_years"] = {}
    for year in range(2021, 2027):
        a, b = stamp(f"{year}-01-01"), min(end, stamp(f"{year + 1}-01-01"))
        replay = run(a, b)
        result["calendar_years"][year] = {
            "portfolio": metrics(replay.curve, CAPITAL), "coinm_entries": replay.stats["coin_entries"],
            "btc_spot_momentum": metrics(spot_momentum(a, b), CAPITAL)}
    result["leave_one_out"] = {}
    for dropped in ALTS:
        subset = Data(alts=[s for s in ALTS if s != dropped])
        result["leave_one_out"][dropped] = metrics(Portfolio(subset).run(start, end).curve, CAPITAL)
    result["capital_scale"] = {}
    for scale in (0.9, 1.0, 1.1, 2.0, 3.0):
        replay = run(spot_btc=0.0012 * scale, coinm_btc=0.0018 * scale)
        result["capital_scale"][str(scale)] = {**metrics(replay.curve, CAPITAL * scale),
                                               "coinm_entries": replay.stats["coin_entries"]}
    spot_pnl = sorted((x["pnl_btc"] for x in base.trades if x["venue"] == "spot" and x["pnl_btc"] is not None), reverse=True)
    coin_pnl = sorted((x["pnl_btc"] for x in base.trades if x["venue"] == "coinm"), reverse=True)
    result["concentration"] = {
        "total_gain_btc": base.curve[-1][1] - CAPITAL,
        "alt_realized_btc": sum(spot_pnl), "alt_top3_btc": sum(spot_pnl[:3]), "alt_rest_btc": sum(spot_pnl[3:]),
        "alt_trades": len(spot_pnl), "alt_winners": sum(p > 0 for p in spot_pnl),
        "coinm_realized_btc": sum(coin_pnl), "coinm_top2_btc": sum(coin_pnl[:2]), "coinm_rest_btc": sum(coin_pnl[2:]),
        "coinm_trades": len(coin_pnl), "coinm_winners": sum(p > 0 for p in coin_pnl),
        "first_coinm_trade": next((x["time"] for x in base.trades if x["venue"] == "coinm"), None),
        "top_alt_trades": sorted((x for x in base.trades if x["venue"] == "spot" and x["pnl_btc"] is not None),
                                 key=lambda x: -x["pnl_btc"])[:5]}
    result["final_holdings"] = {"wallet": base.wallet, "coinm_wallet_btc": base.coin_wallet,
                                "coinm_contracts": base.coin_qty}
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(result, indent=1, default=str))
    with open(output / "trades.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, ["venue", "symbol", "reason", "time", "entry", "exit", "quantity", "pnl_btc"])
        writer.writeheader()
        writer.writerows(base.trades)
    with open(output / "equity_daily.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day_utc", "portfolio_btc"])
        writer.writerows((date(t)[:10], f"{v:.8f}") for t, v in base.curve)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("download", "study"))
    args = parser.parse_args(argv)
    if args.command == "download":
        download()
    else:
        result = study(Data())
        print(json.dumps({k: result[k] for k in ("main", "concentration")}, indent=1, default=str))


if __name__ == "__main__":
    main()
