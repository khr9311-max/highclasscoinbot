"""Exact-rule replay of the aggressive BTC portfolio at 5-minute resolution.

Spot sleeve: the whole sleeve in the strongest of ETH/BNB/SOL/XRP against BTC
(completed-day 20/60-day average, as btc_portfolio.signals.alt_signal), or BTC
when none is positive. Decided at 00:00 UTC and filled at that 5-minute open
with IOC slippage, half a tick, lot steps, minimum notional and the buy fee
taken from the received asset.

COIN-M sleeve: BTCUSD_PERP inverse perpetual, direction from EMA20/80 over the
200 completed 4h bars (as btc_portfolio.signals.coinm_signal), always in the
market. Whole contracts sized at entry as floor(L x sleeve BTC x price / 100)
and held until the next flip (a resizing rule can be selected). The leverage
L comes from a fixed value or a declared rule whose constants are calibrated
on the development period only. Exchange-held MARK_PRICE disaster stop,
isolated-margin liquidation from the contract's maintenance margin, funding,
taker fees. After a stop or liquidation the same direction is not re-entered
until the signal flips.

Sleeves: optional monthly internal BTC transfer on the 1st at 00:00 UTC back to
the target split when it drifted by 5 percentage points, selling or buying the
held alt as needed. Kill switch: at 25% of the starting total, no new entries,
purchases, transfers or leverage increases; exits and stops still run.

Assumptions: IOC orders fill completely; the 5-minute open stands in for the
first 30-second poll; today's spot filters and contract rules apply to the
whole history. All results are BTC quantities. No credentials are used.
"""
from __future__ import annotations

import argparse
import calendar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from btc_lab import strategy_search as ss

ROOT = Path(__file__).resolve().parents[1]
MARK = ROOT / "btc_lab/state/intraday_market/BTCUSD_PERP_mark_5m.npz"
FILTERS = ROOT / "btc_lab/state/portfolio_market/spot_filters.json"
COIN_SPEC = ROOT / "btc_lab/state/portfolio_market/coinm_spec.json"
OUTPUT = ROOT / "btc_lab/state/aggressive_backtest_20260925"
ALTS = ss.ALTS
B4, BD = 48, 288
FEE_SPOT, FEE_COIN, SLIP, STOP_SLIP = 0.001, 0.0005, 0.0003, 0.0005
EXCHANGE_LEVERAGE = 3
MIN_STOP_GAP = 0.05
PERIODS = {"dev": ss.DEV, "holdout": ss.HOLDOUT, "full": (ss.DEV[0], ss.HOLDOUT[1])}


def window_ema(values, span, window=200):
    """EMA seeded at the first of the `window` bars completed before each bar."""
    alpha = 2 / (span + 1)
    weights = np.concatenate(([(1 - alpha) ** (window - 1)],
                              alpha * (1 - alpha) ** np.arange(window - 1)[::-1]))
    out = np.full(len(values), np.nan)
    out[window:] = sliding_window_view(values, window)[:-1] @ weights
    return out


def liquidation_price(entry, direction, mmr, leverage=EXCHANGE_LEVERAGE):
    """Isolated inverse position with margin = notional / leverage."""
    if direction > 0:
        return entry * (1 + mmr) / (1 + 1 / leverage)
    return entry * (1 - mmr) / (1 - 1 / leverage)


class Data:
    def __init__(self, market=None):
        m = self.m = market or ss.Market()
        self.n, self.t0 = m.n, m.t0
        grid = m.t0 + 300_000 * np.arange(m.n, dtype=np.int64)
        raw = np.load(MARK)
        frame = pd.DataFrame({k: raw[k] for k in ("open", "high", "low", "close")},
                             index=raw["open_ms"]).reindex(grid)
        close = frame["close"].ffill().bfill()
        for k in ("open", "high", "low"):
            frame[k] = frame[k].fillna(close)
        self.mark = {k: frame[k].to_numpy() for k in ("open", "high", "low")}
        self.coin = m.series["BTCUSD_PERP"]
        self.alt = {s: m.series[s] for s in ALTS}
        self.funding = m.funding
        filters = json.loads(FILTERS.read_text())
        pick = lambda s, kind: next(f for f in filters[s] if f["filterType"] == kind)
        self.step = {s: float(pick(s, "LOT_SIZE")["stepSize"]) for s in ALTS}
        self.min_notional = {s: float(pick(s, "NOTIONAL")["minNotional"]) for s in ALTS}
        self.mmr = float(json.loads(COIN_SPEC.read_text())["maintMarginPercent"]) / 100
        c4 = m.bars("BTCUSD_PERP", "4h")["close"]
        self.fast, self.slow = window_ema(c4, 20), window_ema(c4, 80)
        lr = np.diff(np.log(c4), prepend=np.nan)
        self.vol = ss.prior(pd.Series(lr).rolling(180).std().to_numpy())     # prior 30 days of 4h bars
        self.spread = np.abs(self.fast / self.slow - 1)
        self.dclose = {s: self.alt[s]["close"][BD - 1::BD] for s in ALTS}
        btc_daily = self.coin["close"][BD - 1::BD]
        self.btc_daily = btc_daily
        self.sma200 = pd.Series(btc_daily).rolling(200).mean().to_numpy()
        dev = slice((ss.stamp(ss.DEV[0]) - m.t0) // 300_000 // B4, (ss.stamp(ss.DEV[1]) - m.t0) // 300_000 // B4)
        self.vol_ref = float(np.nanmedian(self.vol[dev]))       # calibrated on the development period only
        self.spread_ref = float(np.nanmedian(self.spread[dev]))

    def direction(self, i):
        diff = self.fast[i] - self.slow[i]
        return 0 if not np.isfinite(diff) or diff == 0 else (1 if diff > 0 else -1)

    def alt_winner(self, day):
        if day < 61:
            return None, {}
        scores = {}
        for s in ALTS:
            c = self.dclose[s]
            latest = c[day - 1]
            scores[s] = (latest / c[day - 21] - 1 + latest / c[day - 61] - 1) / 2
        winner = max(sorted(scores), key=scores.get)
        return (winner if scores[winner] > 0 else None), scores


@dataclass(frozen=True)
class Config:
    spot_fraction: float = 0.5
    capital_btc: float = 0.003
    leverage: str = "fixed"       # fixed | vol | regime | drawdown | vol_drawdown | trend
    lev: float = 2.0              # fixed level, or the base of a rule
    lev_min: float = 1.0
    lev_max: float = 3.0
    resize: str = "entry"         # entry | daily | bar (every 4h bar)
    stop: float | None = 0.12
    stop_reentry: str = "flip"   # flip: wait for the opposite signal | next_bar: re-enter if unchanged
    rebalance: str = "monthly"    # none | monthly
    kill: float = 0.25
    cost_mult: float = 1.0


class Replay:
    def __init__(self, data: Data, cfg: Config):
        self.d, self.c = data, cfg
        self.spot_btc = cfg.capital_btc * cfg.spot_fraction
        self.coin_wallet = cfg.capital_btc - self.spot_btc
        self.qty = {s: 0.0 for s in ALTS}
        self.held = None
        self.contracts = 0            # signed whole contracts
        self.entry = self.stop_px = self.liq_px = None
        self.blocked = None
        self.kill_day = None
        self.initial = cfg.capital_btc
        self.peak = cfg.capital_btc
        self.records = []
        self.entry_leverage = []
        self.n = {k: 0 for k in ("alt_buys", "alt_sells", "coin_entries", "flips", "stops", "liquidations",
                                 "size_skips", "resizes", "transfers", "rule_skips")}
        self.fees = self.funding = 0.0
        self.events = []

    # ------------------------------------------------------------ valuation
    def half_tick(self, s, price):
        return 0.5 * ss.TICK[s] * self.c.cost_mult

    def spot_equity(self, t):
        value = self.spot_btc
        for s in ALTS:
            if self.qty[s]:
                p = self.d.alt[s]["open"][t]
                value += self.qty[s] * (p - self.half_tick(s, p))
        return value

    def unrealized(self, mark):
        if not self.contracts:
            return 0.0
        side = 1 if self.contracts > 0 else -1
        return side * abs(self.contracts) * 100 * (1 / self.entry - 1 / mark)

    def coin_equity(self, t):
        return self.coin_wallet + self.unrealized(self.d.mark["open"][t])

    def total(self, t):
        return self.spot_equity(t) + self.coin_equity(t)

    # ------------------------------------------------------------ spot sleeve
    def alt_sell(self, s, t, quantity=None):
        step = self.d.step[s]
        qty = math.floor((self.qty[s] if quantity is None else min(quantity, self.qty[s])) / step + 1e-9) * step
        p = self.d.alt[s]["open"][t]
        price = p * (1 - SLIP * self.c.cost_mult) - self.half_tick(s, p)
        if qty <= 0 or qty * price < self.d.min_notional[s]:
            return False
        fee = qty * price * FEE_SPOT * self.c.cost_mult
        self.qty[s] -= qty
        self.spot_btc += qty * price - fee
        self.fees += fee
        self.n["alt_sells"] += 1
        return True

    def alt_buy(self, s, t):
        step = self.d.step[s]
        p = self.d.alt[s]["open"][t]
        price = p * (1 + SLIP * self.c.cost_mult) + self.half_tick(s, p)
        qty = math.floor(self.spot_btc / price / step + 1e-9) * step
        if qty <= 0 or qty * price < self.d.min_notional[s]:
            return False
        self.spot_btc -= qty * price
        fee = qty * FEE_SPOT * self.c.cost_mult
        self.qty[s] += qty - fee                     # buy commission in the received asset
        self.fees += fee * price
        self.n["alt_buys"] += 1
        return True

    def spot_day(self, t, day, kill):
        winner, _ = self.d.alt_winner(day)
        if self.held and winner != self.held:
            if self.alt_sell(self.held, t):
                self.held = None
        if winner and winner != self.held and not kill and self.held is None:
            if self.alt_buy(winner, t):
                self.held = winner

    # ------------------------------------------------------------ COIN-M sleeve
    def leverage(self, i, t, day, direction):
        c, d = self.c, self.d
        if c.leverage == "fixed":
            return c.lev
        if c.leverage == "vol":
            v = d.vol[i]
            return c.lev if not np.isfinite(v) else float(np.clip(c.lev * d.vol_ref / v, c.lev_min, c.lev_max))
        if c.leverage == "regime":
            ref = d.sma200[day - 1] if day >= 1 else np.nan
            if not np.isfinite(ref):
                return c.lev
            trend = 1 if d.btc_daily[day - 1] > ref else -1
            return c.lev_max if direction == trend else c.lev_min
        if c.leverage == "drawdown":
            return c.lev if self.total(t) >= 0.7 * self.peak else c.lev_min
        if c.leverage == "vol_drawdown":
            v = d.vol[i]
            lev = c.lev if not np.isfinite(v) else float(np.clip(c.lev * d.vol_ref / v, c.lev_min, c.lev_max))
            return lev if self.total(t) >= 0.7 * self.peak else min(lev, 1.0)
        if c.leverage == "trend":
            s = d.spread[i]
            return c.lev if not np.isfinite(s) else float(np.clip(1 + s / d.spread_ref, c.lev_min, c.lev_max))
        raise ValueError("Unknown leverage rule")

    def margin(self):
        return abs(self.contracts) * 100 / self.entry / EXCHANGE_LEVERAGE if self.contracts else 0.0

    def close(self, t, price, reason):
        side = 1 if self.contracts > 0 else -1
        notional = abs(self.contracts) * 100
        fee = notional / price * FEE_COIN * self.c.cost_mult
        self.coin_wallet += side * notional * (1 / self.entry - 1 / price) - fee
        self.fees += fee
        self.contracts, self.entry, self.stop_px, self.liq_px = 0, None, None, None
        if reason in ("stop", "liquidation"):
            self.blocked = side if self.c.stop_reentry == "flip" else None
            self.events.append((t, reason, side, price))
            self.n["stops" if reason == "stop" else "liquidations"] += 1

    def open(self, i, t, day, direction):
        price = self.d.coin["open"][t] * (1 + direction * SLIP * self.c.cost_mult)
        lev = self.leverage(i, t, day, direction)
        equity = self.coin_wallet
        contracts = math.floor(lev * equity * price / 100 + 1e-9)
        fee_rate = FEE_COIN * self.c.cost_mult
        while contracts > 0 and contracts * 100 / price * (1 / EXCHANGE_LEVERAGE + fee_rate) > self.coin_wallet:
            contracts -= 1
        if contracts < 1:
            self.n["size_skips"] += 1
            return
        stop = None
        liq = liquidation_price(price, direction, self.d.mmr)
        if self.c.stop is not None:
            stop = price * (1 - direction * self.c.stop)
            if direction * (stop - liq) / price < MIN_STOP_GAP:
                self.n["rule_skips"] += 1
                return
        fee = contracts * 100 / price * fee_rate
        self.coin_wallet -= fee
        self.fees += fee
        self.contracts, self.entry, self.stop_px, self.liq_px = direction * contracts, price, stop, liq
        self.entry_leverage.append(contracts * 100 / price / equity)
        self.n["coin_entries"] += 1

    def resize(self, i, t, day, kill):
        side = 1 if self.contracts > 0 else -1
        mark = self.d.mark["open"][t]
        equity = self.coin_equity(t)
        if equity <= 0:
            return
        lev = self.leverage(i, t, day, side)
        target = math.floor(lev * equity * mark / 100 + 1e-9)
        current = abs(self.contracts)
        threshold = 1 if self.c.resize == "bar" else max(1, round(0.25 * current))
        if abs(target - current) < threshold:
            return
        fee_rate = FEE_COIN * self.c.cost_mult
        if target > current and not kill:
            price = self.d.coin["open"][t] * (1 + side * SLIP * self.c.cost_mult)
            add = target - current
            free = self.coin_wallet - self.margin()
            while add > 0 and add * 100 / price * (1 / EXCHANGE_LEVERAGE + fee_rate) > free:
                add -= 1
            if add <= 0:
                return
            new_entry = (current + add) / (current / self.entry + add / price)
            liq = liquidation_price(new_entry, side, self.d.mmr)
            if self.stop_px is not None and side * (self.stop_px - liq) / new_entry < MIN_STOP_GAP:
                self.n["rule_skips"] += 1
                return
            fee = add * 100 / price * fee_rate
            self.coin_wallet -= fee
            self.fees += fee
            self.contracts, self.entry, self.liq_px = side * (current + add), new_entry, liq
            self.n["resizes"] += 1
        elif target < current and target >= 1:
            price = self.d.coin["open"][t] * (1 - side * SLIP * self.c.cost_mult)
            cut = current - target
            fee = cut * 100 / price * fee_rate
            self.coin_wallet += side * cut * 100 * (1 / self.entry - 1 / price) - fee
            self.fees += fee
            self.contracts = side * target
            self.n["resizes"] += 1

    def intrabar(self, t, end):
        if not self.contracts:
            return
        k = np.arange(t, min(t + B4, end))
        side = 1 if self.contracts > 0 else -1
        mo, mh, ml = self.d.mark["open"][k], self.d.mark["high"][k], self.d.mark["low"][k]
        if side > 0:
            liq_hit = (mo <= self.liq_px) | ((ml <= self.liq_px) if self.stop_px is None else False)
            stop_hit = ml <= self.stop_px if self.stop_px is not None else np.zeros(len(k), bool)
        else:
            liq_hit = (mo >= self.liq_px) | ((mh >= self.liq_px) if self.stop_px is None else False)
            stop_hit = mh >= self.stop_px if self.stop_px is not None else np.zeros(len(k), bool)
        event = liq_hit | stop_hit
        first = int(np.argmax(event)) if event.any() else len(k)
        # funding settles on the open position until the protective event
        for j in np.flatnonzero(self.d.funding[k[:first]]):
            rate = self.d.funding[k[j]]
            pay = side * abs(self.contracts) * 100 / mo[j] * rate
            self.coin_wallet -= pay
            self.funding += pay
        if first == len(k):
            return
        j = first
        if liq_hit[j]:
            self.coin_wallet -= self.margin()           # isolated margin is lost; the fee comes out of it
            self.contracts, self.entry, self.stop_px, self.liq_px = 0, None, None, None
            self.blocked = side
            self.n["liquidations"] += 1
            return
        opening = self.d.coin["open"][k[j]]
        fill = (min(self.stop_px, opening) * (1 - STOP_SLIP * self.c.cost_mult) if side > 0
                else max(self.stop_px, opening) * (1 + STOP_SLIP * self.c.cost_mult))
        self.close(k[j], fill, "stop")

    # ------------------------------------------------------------ sleeves
    def rebalance(self, t):
        spot, coin = self.spot_equity(t), self.coin_equity(t)
        total = spot + coin
        if total <= 0 or abs(spot / total - self.c.spot_fraction) < 0.05:
            return
        diff = self.c.spot_fraction * total - spot
        if diff > 0:                                      # COIN-M -> spot
            amount = diff
            available = self.coin_wallet - self.margin() - 0.00001
            amount = min(amount, max(0.0, available))
            if self.contracts:
                exposure = abs(self.contracts) * 100 / self.d.mark["open"][t]
                cap = max(2.5, self.c.lev if self.c.leverage == "fixed" else self.c.lev_max)
                amount = min(amount, max(0.0, coin - exposure / cap))
            if amount <= 0:
                return
            self.coin_wallet -= amount
            self.spot_btc += amount
            if self.held:
                self.alt_buy(self.held, t)
        else:                                             # spot -> COIN-M
            need = -diff
            if self.spot_btc < need and self.held:
                p = self.d.alt[self.held]["open"][t]
                unit = p * (1 - SLIP * self.c.cost_mult) * (1 - FEE_SPOT * self.c.cost_mult)
                self.alt_sell(self.held, t, (need - self.spot_btc) / unit + self.d.step[self.held])
                if self.qty[self.held] * p < self.d.min_notional[self.held]:
                    self.held = None
            amount = min(need, self.spot_btc)
            if amount <= 0:
                return
            self.spot_btc -= amount
            self.coin_wallet += amount
        self.n["transfers"] += 1

    def run(self, start, end):
        a = (ss.stamp(start) - self.d.t0) // 300_000
        b = min((ss.stamp(end) - self.d.t0) // 300_000, self.d.n)
        a = -(-a // B4) * B4
        for i in range(a // B4, b // B4):
            t = i * B4
            kill = self.kill_day is not None
            new_day = t % BD == 0
            day = t // BD
            if new_day:
                equity = self.total(t)
                self.records.append((t, equity))
                self.peak = max(self.peak, equity)
                if not kill and equity <= self.c.kill * self.initial:
                    self.kill_day, kill = day, True
                if self.c.spot_fraction > 0:
                    self.spot_day(t, day, kill)
            direction = self.d.direction(i)
            if self.contracts and direction and direction != (1 if self.contracts > 0 else -1):
                side = 1 if self.contracts > 0 else -1
                self.close(t, self.d.coin["open"][t] * (1 - side * SLIP * self.c.cost_mult), "flip")
                self.n["flips"] += 1
            if new_day and self.c.rebalance == "monthly" and not kill and self.c.spot_fraction not in (0, 1):
                stamp = datetime.fromtimestamp((self.d.t0 + t * 300_000) / 1000, timezone.utc)
                if stamp.day == 1:
                    self.rebalance(t)
            if self.c.spot_fraction < 1 and not self.contracts and direction and not kill:
                if self.blocked is not None and direction == self.blocked:
                    pass
                else:
                    self.blocked = None
                    self.open(i, t, day, direction)
            elif self.contracts and (self.c.resize == "bar" or (new_day and self.c.resize == "daily")):
                self.resize(i, t, day, kill)
            self.intrabar(t, b)
        last = b - 1
        final = self.spot_btc + self.coin_wallet
        for s in ALTS:
            final += self.qty[s] * self.d.alt[s]["close"][last]
        if self.contracts:
            final += self.unrealized(self.d.mark["open"][last])
        self.records.append((b, final))
        return self


def metrics(replay: Replay):
    days = np.array([t for t, _ in replay.records]) // BD
    eq = np.array([v for _, v in replay.records])
    start = replay.initial
    series = np.concatenate(([start], eq))
    peak = np.maximum.accumulate(series)
    dd = 1 - series / peak
    daily = series[1:] / series[:-1] - 1
    under, longest, since = 0, 0, 0
    for k, v in enumerate(series):
        if v >= peak[k]:
            since = k
        longest = max(longest, k - since)
    years = max((days[-1] - days[0]) / 365.25, 1e-9)
    roll = lambda w: float(np.min(series[w:] / series[:-w] - 1)) if len(series) > w else None
    stamps = [datetime.fromtimestamp((replay.d.t0 + t * 300_000) / 1000, timezone.utc) for t, _ in replay.records]
    yearly, first = {}, {}
    for when, v in zip(stamps, eq):
        first.setdefault(when.year, v)
    ordered = sorted(first)
    for y, nxt in zip(ordered, ordered[1:] + [None]):
        yearly[y] = (first[nxt] if nxt else eq[-1]) / first[y] - 1
    lev = np.array(replay.entry_leverage) if replay.entry_leverage else None
    return {"return": float(eq[-1] / start - 1), "cagr": float((eq[-1] / start) ** (1 / years) - 1),
            "max_dd": float(dd.max()), "worst_day": float(daily.min()), "worst_30d": roll(30),
            "worst_365d": roll(365), "longest_underwater_days": int(longest), "final_btc": float(eq[-1]),
            "yearly": {str(k): float(v) for k, v in yearly.items()}, "fees_btc": replay.fees,
            "funding_btc": replay.funding, "kill_day": replay.kill_day,
            "entry_leverage": None if lev is None else {"mean": float(lev.mean()), "min": float(lev.min()), "max": float(lev.max())},
            **replay.n}


def run(data, cfg, period):
    return metrics(Replay(data, cfg).run(*PERIODS[period]))


def cases():
    base = Config()
    out = {
        "alt_only": replace(base, spot_fraction=1.0, rebalance="none"),
        "coinm_only_2x": replace(base, spot_fraction=0.0, rebalance="none"),
        "combo_5050_2x_none": replace(base, rebalance="none"),
        "combo_5050_2x_monthly": base,
        "combo_4060_2x_none": replace(base, spot_fraction=0.4, rebalance="none"),
        "combo_4060_2x_monthly": replace(base, spot_fraction=0.4),
    }
    for lev in (1.0, 1.5, 2.5, 3.0):
        out[f"combo_5050_{lev:g}x_monthly"] = replace(base, lev=lev)
    out["coinm_only_2x_resize_daily"] = replace(base, spot_fraction=0.0, rebalance="none", resize="daily")
    out["combo_5050_2x_resize_daily"] = replace(base, resize="daily")
    out["combo_5050_2x_resize_bar"] = replace(base, resize="bar")
    out["combo_5050_2x_nostop"] = replace(base, stop=None)
    out["combo_5050_2x_stop15"] = replace(base, stop=0.15)
    out["combo_5050_2x_stop_nextbar"] = replace(base, stop_reentry="next_bar")
    out["dyn_vol_entry"] = replace(base, leverage="vol")
    out["dyn_vol_daily"] = replace(base, leverage="vol", resize="daily")
    out["dyn_regime_entry"] = replace(base, leverage="regime", lev_min=1.5, lev_max=2.5)
    out["dyn_drawdown_entry"] = replace(base, leverage="drawdown")
    out["dyn_trend_daily"] = replace(base, leverage="trend", resize="daily")
    out["dyn_vol_drawdown_daily"] = replace(base, leverage="vol_drawdown", resize="daily")
    out["dyn_vol_daily_none"] = replace(base, leverage="vol", resize="daily", rebalance="none")
    out["combo_5050_1.5x_none"] = replace(base, lev=1.5, rebalance="none")
    return out


def study(output=OUTPUT):
    data = Data()
    rows = {}
    for name, cfg in cases().items():
        for scale in (0.003, 1.0):
            for cost in (1.0, 2.0):
                if cost == 2.0 and name not in ("alt_only", "combo_5050_2x_monthly", "combo_5050_2x_none",
                                                "combo_5050_1.5x_monthly", "dyn_vol_daily", "dyn_drawdown_entry",
                                                "dyn_vol_drawdown_daily", "dyn_vol_entry", "dyn_regime_entry"):
                    continue
                c = replace(cfg, capital_btc=scale, cost_mult=cost)
                key = f"{name}|{scale:g}BTC|cost{cost:g}"
                rows[key] = {"config": asdict(c), **{p: run(data, c, p) for p in PERIODS}}
                r = rows[key]
                print(f"{key:48} dev {r['dev']['return']:+8.1%} ({r['dev']['max_dd']:.0%}) "
                      f"hold {r['holdout']['return']:+8.1%} ({r['holdout']['max_dd']:.0%}) "
                      f"full {r['full']['return']:+8.1%} ({r['full']['max_dd']:.0%})", flush=True)
    result = {"periods": PERIODS, "vol_ref": data.vol_ref, "spread_ref": data.spread_ref, "mmr": data.mmr,
              "rows": rows}
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    study()


if __name__ == "__main__":
    main()
