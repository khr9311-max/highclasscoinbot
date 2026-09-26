"""Pre-registered test: can a much wider alt universe, volume-surge entries or a learned
alt model grow BTC faster than the live four-alt rotation?

Fixed on 2026-09-26 before any result was read:
- Data: alt_data (hourly, 2022-01..2026-08, delisted pairs included). Prices in BTC.
  Signals use completed hours only; every order fills at the next hour's open.
- Universe U(t): base assets listed >= 14 days whose median daily USD volume over the
  prior 7 days is >= $2M. U_btc(t): the subset whose BTC pair did >= 3 BTC/day.
- Costs per side: 0.10% fee per exchange leg (one leg on the BTC pair, two via USDT:
  BTC->USDT->alt), plus half the spread predicted from 24h volume by a log-log fit to
  today's book (x1.5). The BTC pair is used when it did >= 3 BTC/day and is cheaper.
  Surge and model entries pay a further 0.10% for chasing a move; stops fill at the
  stop or a worse open, minus 0.20%. A held pair that stops trading exits at its last
  close minus 2%.
- Strategies (14; LIVE is the baseline, the other 13 are candidates):
  LIVE  ETH/BNB/SOL/XRP, daily top-1 by mean of 20d and 60d BTC return if > 0 (the live rule).
  ROT1  the live rule on U, top-1.   ROT3  the live rule on U, top-3 equal weight.
  MOM7  U, daily top-3 by 7-day return if > 0.
  SURGE_K_X_H  hourly: volume of the hour >= K x median hourly volume of the prior
        168 hours and the hour's BTC return >= X; ranked by the volume ratio; 3 slots of
        a third of equity; exit after H hours or at an 8% stop. K 5|10, X 3%|6%, H 6|24.
  MODEL_H  LightGBM regression of the next-H-hour BTC log return (clipped +-50%) on
        21 features of U(t) rows (returns, volume surges, taker share, range, volatility,
        distance from 7d high, age, BTC's own move, cross-sectional ranks, hour).
        Trained every 3 months on all earlier rows whose target ended before the
        training date (rows every 4th hour), first model at 2023-01-01. Enter when the
        prediction exceeds the round-trip cost + 0.5%; best first, 3 slots, exit after
        H hours. H 6|24.
- Periods: DEV 2023-01-01..2025-01-01, HOLDOUT 2025-01-01..2026-09-01.
- Selection: the candidate with the highest DEV BTC multiple among those with >= 30
  DEV trades. Adopted only if in HOLDOUT its BTC multiple is > 1 and > LIVE's, and its
  max drawdown is <= 60%. All candidates' HOLDOUT results are reported but do not decide.
"""
from __future__ import annotations

import argparse
import json
import math
import time

import numpy as np

from btc_lab import alt_data

OUTPUT = alt_data.ROOT / "btc_lab/state/alt_search_20260926"
HOUR = alt_data.HOUR
DAY = 24
FEE = 0.001
SPREAD_MULT = 1.5
CHASE = 0.001
STOP_SLIP = 0.002
DELIST_HAIRCUT = 0.02
MIN_USD = 2e6
MIN_BTC_PAIR = 3.0
MIN_AGE = 14 * DAY
SLOTS = 3
STOP = 0.08
PERIODS = {"dev": ("2023-01-01", "2025-01-01"), "holdout": ("2025-01-01", "2026-09-01")}
LIVE_BASES = ("ETH", "BNB", "SOL", "XRP")
SURGE_GRID = [(k, x, h) for k in (5, 10) for x in (0.03, 0.06) for h in (6, 24)]
MODEL_HORIZONS = (6, 24)
RETRAIN_MONTHS = 3


def stamp(day):
    return int(np.datetime64(day, "ms").astype(np.int64))


# ---------------------------------------------------------------- panel

def rolling_sum(x, n):
    """Trailing n-hour sum ending at each hour (NaN treated as 0); axis 1 is time."""
    c = np.cumsum(np.nan_to_num(x, nan=0.0), axis=1, dtype=np.float64)
    out = c.copy()
    out[:, n:] -= c[:, :-n]
    return out


def lag(x, k):
    out = np.full_like(x, np.nan)
    if k < x.shape[1]:
        out[:, k:] = x[:, :-k] if k else x
    return out


def rolling_median(x, n):
    """Median of x over the n hours before each hour (at least half present)."""
    import pandas as pd
    return pd.DataFrame(x.T).shift(1).rolling(n, min_periods=n // 2).median().to_numpy(np.float32).T.copy()


def rolling_max(x, n):
    """Maximum of x over the n hours ending at each hour."""
    import pandas as pd
    return pd.DataFrame(x.T).rolling(n, min_periods=n // 2).max().to_numpy(np.float32).T.copy()


class Panel:
    def __init__(self, raw, book):
        self.bases = [str(b) for b in raw["bases"]]
        self.open_ms = raw["open_ms"]
        f = lambda k: raw[k].astype(np.float32)
        self.o, self.h, self.l, self.c = f("open"), f("high"), f("low"), f("close")
        self.v = np.nan_to_num(f("volume_usd"))
        self.taker = f("taker_buy_share")
        self.btc_pair = np.nan_to_num(f("btc_pair_volume_btc"))
        self.btc_usd = raw["btc_usd"].astype(np.float64)
        listed = np.isfinite(self.c) & (self.c > 0)
        self.listed = listed
        self.age = np.cumsum(listed, axis=1)
        v24 = rolling_sum(self.v, DAY)
        daily = np.stack([lag(v24, k) for k in range(0, 7 * DAY, DAY)])
        self.liq = np.median(daily, axis=0).astype(np.float32)           # median daily USD volume, 7 days
        p24 = rolling_sum(self.btc_pair, DAY)
        self.liq_btc_pair = np.median(np.stack([lag(p24, k) for k in range(0, 7 * DAY, DAY)]), axis=0).astype(np.float32)
        self.v24 = v24.astype(np.float32)
        self.universe = listed & (self.age >= MIN_AGE) & (self.liq >= MIN_USD)
        self.universe_btc = self.universe & (self.liq_btc_pair >= MIN_BTC_PAIR)
        self.cost = self.side_cost(book)

    def side_cost(self, book):
        """Per-side cost (fraction) by hour and asset, with the cheaper eligible route."""
        fits = {}
        for quote in ("USDT", "BTC"):
            rows = [(v["quote_volume_24h"], v["spread"]) for s, v in book.items() if s.endswith(quote) and v["spread"] > 0]
            if quote == "BTC":
                rows = [(q * float(np.nanmean(self.btc_usd[-DAY:])), s) for q, s in rows]
            x, y = np.log([r[0] for r in rows]), np.log([r[1] for r in rows])
            fits[quote] = np.polyfit(x, y, 1)
        spread = lambda fit, usd: np.clip(np.exp(np.polyval(fit, np.log(np.maximum(usd, 1.0)))), 1e-4, 0.02)
        via_usdt = 2 * FEE + SPREAD_MULT * spread(fits["USDT"], self.v24) / 2
        btc_usd24 = self.btc_pair_24h_usd()
        via_btc = FEE + SPREAD_MULT * spread(fits["BTC"], btc_usd24) / 2
        use_btc = (self.liq_btc_pair >= MIN_BTC_PAIR) & (via_btc < via_usdt)
        self.fits = {q: [float(a) for a in f] for q, f in fits.items()}
        return np.where(use_btc, via_btc, via_usdt).astype(np.float32)

    def btc_pair_24h_usd(self):
        return rolling_sum(self.btc_pair, DAY) * self.btc_usd[None, :]

    def index(self, day):
        return int(np.searchsorted(self.open_ms, stamp(day)))


# ---------------------------------------------------------------- simulation

class Book:
    """BTC cash plus positions valued at each hour's close."""

    def __init__(self, equity=1.0):
        self.cash, self.pos, self.trades = equity, {}, []

    def value(self, p, t):
        total = self.cash
        for b, q in self.pos.items():
            px = p.c[b, t]
            total += q["units"] * (px if np.isfinite(px) else q["last"])
        return total

    def buy(self, p, b, t, budget, extra=0.0, **meta):
        px = p.o[b, t]
        if not np.isfinite(px) or px <= 0 or budget <= 0:
            return False
        cost = p.cost[b, t - 1] + extra
        units = budget * (1 - cost) / px
        self.cash -= budget
        self.pos[b] = {"units": units, "entry": px, "t": t, "paid": budget, "last": px, **meta}
        return True

    def sell(self, p, b, t, px=None, extra=0.0, reason="exit"):
        q = self.pos.pop(b)
        px = p.o[b, t] if px is None else px
        if not np.isfinite(px) or px <= 0:
            px, extra = q["last"], extra + DELIST_HAIRCUT
            reason = "delisted"
        cost = p.cost[b, min(t, p.cost.shape[1] - 1) - 1] + extra
        got = q["units"] * px * (1 - cost)
        self.cash += got
        self.trades.append({"base": p.bases[b], "t_in": int(q["t"]), "t_out": int(t), "ret": got / q["paid"] - 1,
                            "reason": reason})

    def mark(self, p, t):
        for b, q in self.pos.items():
            if np.isfinite(p.c[b, t]):
                q["last"] = p.c[b, t]


def rotation(p, start, end, pick):
    """Daily target weights from pick(t_signal) -> list of bases (equal weight); trades at 00:00 opens."""
    book, curve = Book(), []
    for t in range(start, end):
        if (p.open_ms[t] // HOUR) % DAY == 0:
            target = pick(t - 1)
            for b in [b for b in book.pos if b not in target]:
                book.sell(p, b, t, reason="rotate")
            new = [b for b in target if b not in book.pos]
            if new:
                # No resize: kept holdings drift, new ones share the BTC freed by the sales equally.
                budget = book.cash / len(new)
                for b in new:
                    book.buy(p, b, t, budget)
        for b in [b for b in book.pos if not np.isfinite(p.c[b, t])]:
            book.sell(p, b, t, px=np.nan)
        book.mark(p, t)
        curve.append(book.value(p, t))
    return np.asarray(curve), book.trades


def slots(p, start, end, candidates, hold, stop=None, chase=CHASE):
    """candidates(t) -> ranked bases signalled at the close of t; filled at the open of t+1."""
    book, curve, queued = Book(), [], []
    for t in range(start, end):
        for b in [b for b, q in book.pos.items() if t - q["t"] >= hold]:
            book.sell(p, b, t, reason="time")
        free = SLOTS - len(book.pos)
        if queued and free > 0:
            equity = book.value(p, t - 1)
            for b in queued:
                if free == 0:
                    break
                if b in book.pos:
                    continue
                if book.buy(p, b, t, min(equity / SLOTS, book.cash), extra=chase,
                            stop=p.o[b, t] * (1 - stop) if stop else None):
                    free -= 1
        for b in list(book.pos):
            q = book.pos[b]
            if not np.isfinite(p.c[b, t]):
                book.sell(p, b, t, px=np.nan)
            elif q.get("stop") and p.l[b, t] <= q["stop"]:
                fill = min(p.o[b, t], q["stop"]) if q["t"] < t else q["stop"]
                book.sell(p, b, t, px=fill, extra=STOP_SLIP, reason="stop")
        book.mark(p, t)
        curve.append(book.value(p, t))
        queued = candidates(t)
    return np.asarray(curve), book.trades


# ---------------------------------------------------------------- strategies

def live_score(p, t, window=None):
    """Mean of 20d and 60d BTC return from daily closes at hour t (a day close)."""
    c, c20, c60 = p.c[:, t], p.c[:, t - 20 * DAY], p.c[:, t - 60 * DAY]
    with np.errstate(all="ignore"):
        return (c / c20 - 1 + c / c60 - 1) / 2


def top(score, mask, k, positive=True):
    s = np.where(mask & np.isfinite(score), score, -np.inf)
    order = np.argsort(-s)[:k]
    return [int(b) for b in order if np.isfinite(s[b]) and (s[b] > 0 or not positive)]


def rotation_picks(p):
    live = np.zeros(len(p.bases), dtype=bool)
    live[[p.bases.index(b) for b in LIVE_BASES]] = True

    def mom7(t):
        with np.errstate(all="ignore"):
            return p.c[:, t] / p.c[:, t - 7 * DAY] - 1
    return {"LIVE": lambda t: top(live_score(p, t), live & p.listed[:, t], 1),
            "ROT1": lambda t: top(live_score(p, t), p.universe[:, t], 1),
            "ROT3": lambda t: top(live_score(p, t), p.universe[:, t], 3),
            "MOM7": lambda t: top(mom7(t), p.universe[:, t], 3)}


def surge_signals(p):
    med = rolling_median(p.v, 7 * DAY)
    with np.errstate(all="ignore"):
        ratio = p.v / np.where(med > 0, med, np.nan)
        ret1 = p.c / lag(p.c, 1) - 1
    return ratio.astype(np.float32), ret1.astype(np.float32)


def surge_candidates(p, ratio, ret1, k, x, universe):
    def pick(t):
        ok = universe[:, t] & (ratio[:, t] >= k) & (ret1[:, t] >= x)
        idx = np.flatnonzero(ok)
        return [int(b) for b in idx[np.argsort(-ratio[idx, t])]]
    return pick


# ---------------------------------------------------------------- learned model

FEATURES = ("r1", "r4", "r24", "r72", "r168", "vol_ratio1", "vol_ratio24", "log_liq", "taker1", "taker24",
            "range1", "rv24", "dist_high168", "log_age", "btc_r1", "btc_r24", "rank_r24", "rank_vol1",
            "rank_r1", "hour", "cost")


def features(p, ratio, ret1):
    lc = np.log(np.where(p.c > 0, p.c, np.nan))
    r = lambda k: (lc - lag(lc, k)).astype(np.float32)
    lr1 = r(1)
    rv = np.sqrt(rolling_sum(np.nan_to_num(lr1) ** 2, DAY) / DAY).astype(np.float32)
    high168 = rolling_max(p.h, 7 * DAY)
    with np.errstate(all="ignore"):
        v24 = p.v24
        vol_ratio24 = v24 / np.where(p.liq > 0, p.liq, np.nan)
        taker_v = rolling_sum(np.nan_to_num(p.taker) * p.v, DAY)
        taker24 = taker_v / np.where(v24 > 0, v24, np.nan)
    btc = np.log(p.btc_usd)
    btc_r = lambda k: np.concatenate([np.full(k, np.nan), btc[k:] - btc[:-k]]).astype(np.float32)
    n_b, n_t = p.c.shape
    F = {"r1": lr1, "r4": r(4), "r24": r(24), "r72": r(72), "r168": r(168),
         "vol_ratio1": np.log1p(np.nan_to_num(ratio)).astype(np.float32),
         "vol_ratio24": np.log1p(np.nan_to_num(vol_ratio24)).astype(np.float32),
         "log_liq": np.log1p(p.liq).astype(np.float32), "taker1": p.taker, "taker24": taker24.astype(np.float32),
         "range1": (p.h / p.l - 1).astype(np.float32), "rv24": rv,
         "dist_high168": (p.c / high168 - 1).astype(np.float32),
         "log_age": np.log1p(np.minimum(p.age, 24 * 365)).astype(np.float32),
         "btc_r1": np.broadcast_to(btc_r(1), (n_b, n_t)), "btc_r24": np.broadcast_to(btc_r(24), (n_b, n_t)),
         "hour": np.broadcast_to(((p.open_ms // HOUR) % DAY).astype(np.float32), (n_b, n_t)),
         "cost": p.cost}
    for name, src in (("rank_r24", F["r24"]), ("rank_vol1", F["vol_ratio1"]), ("rank_r1", F["r1"])):
        masked = np.where(p.universe, src, np.nan)
        with np.errstate(all="ignore"):
            order = np.argsort(np.argsort(np.where(np.isfinite(masked), masked, np.inf), axis=0), axis=0).astype(np.float32)
            count = np.isfinite(masked).sum(axis=0).astype(np.float32)
            F[name] = np.where(np.isfinite(masked), order / np.maximum(count - 1, 1), np.nan).astype(np.float32)
    return F


def forward(p, h):
    """Log BTC return from the open of t+1 to the open of t+1+h, clipped to +-50%."""
    lo = np.log(np.where(p.o > 0, p.o, np.nan))
    out = np.full_like(lo, np.nan)
    out[:, : -(h + 1)] = lo[:, h + 1:] - lo[:, 1: -h]
    return np.clip(out, math.log(0.5), math.log(1.5)).astype(np.float32)


def model_predictions(p, F, h, first="2023-01-01", end=None, threads=16):
    import lightgbm as lgb
    y = forward(p, h)
    X = np.stack([F[k] for k in FEATURES], axis=-1)                         # bases x hours x features
    pred = np.full(p.c.shape, np.nan, dtype=np.float32)
    starts = []
    t = p.index(first)
    end = end or p.c.shape[1]
    while t < end:
        starts.append(t)
        d = np.datetime64(int(p.open_ms[t]), "ms").astype("datetime64[M]") + RETRAIN_MONTHS
        t = int(np.searchsorted(p.open_ms, d.astype("datetime64[ms]").astype(np.int64)))
    log = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end
        train_t = np.arange(MIN_AGE, s - h - 1)
        train_t = train_t[train_t % 4 == 0]
        mask = p.universe[:, train_t] & np.isfinite(y[:, train_t])
        bi, ti = np.nonzero(mask)
        Xt, yt = X[bi, train_t[ti]], y[bi, train_t[ti]]
        model = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=31, min_child_samples=500,
                                  subsample=0.7, subsample_freq=1, colsample_bytree=0.8, reg_lambda=10.0,
                                  n_jobs=threads, verbose=-1)
        model.fit(Xt, yt)
        rows = p.universe[:, s:e]
        bj, tj = np.nonzero(rows)
        pred[bj, s + tj] = model.predict(X[bj, s + tj])
        log.append({"start": str(np.datetime64(int(p.open_ms[s]), "ms"))[:10], "rows": int(len(yt))})
    return pred, log


def model_candidates(p, pred):
    def pick(t):
        edge = pred[:, t] - (2 * p.cost[:, t] + CHASE + 0.005)
        idx = np.flatnonzero(np.isfinite(edge) & (edge > 0))
        return [int(b) for b in idx[np.argsort(-edge[idx])]]
    return pick


# ---------------------------------------------------------------- report

def summary(curve, trades, p, start):
    daily = curve[DAY - 1::DAY]
    rets = np.diff(np.r_[1.0, daily]) / np.r_[1.0, daily[:-1]]
    peak = np.maximum.accumulate(curve)
    days = len(curve) / DAY
    return {"btc_multiple": float(curve[-1]), "cagr": float(curve[-1] ** (365 / days) - 1),
            "max_drawdown": float(1 - (curve / peak).min()),
            "sharpe": float(rets.mean() / rets.std() * math.sqrt(365)) if rets.std() > 0 else 0.0,
            "trades": len(trades), "win_rate": float(np.mean([x["ret"] > 0 for x in trades])) if trades else None,
            "mean_trade": float(np.mean([x["ret"] for x in trades])) if trades else None,
            "stops": sum(x["reason"] == "stop" for x in trades),
            "delisted": sum(x["reason"] == "delisted" for x in trades)}


def run(output=OUTPUT, threads=16):
    started = time.time()
    output.mkdir(parents=True, exist_ok=True)
    raw = alt_data.load()
    book = json.loads((alt_data.OUTPUT / "book_sample.json").read_text())
    p = Panel(raw, book)
    ratio, ret1 = surge_signals(p)
    print(f"panel {len(p.bases)} bases x {p.c.shape[1]} hours; U median size "
          f"{int(np.median(p.universe.sum(axis=0)[p.index('2023-01-01'):]))}, U_btc {int(np.median(p.universe_btc.sum(axis=0)[p.index('2023-01-01'):]))}; "
          f"spread fits {p.fits} ({time.time() - started:.0f}s)", flush=True)
    strategies = {name: ("rotation", pick) for name, pick in rotation_picks(p).items()}
    for k, x, h in SURGE_GRID:
        strategies[f"SURGE_{k}_{int(x * 100)}_{h}"] = ("slots", surge_candidates(p, ratio, ret1, k, x, p.universe), h, STOP)
    F = features(p, ratio, ret1)
    model_log = {}
    for h in MODEL_HORIZONS:
        pred, model_log[h] = model_predictions(p, F, h, threads=threads)
        strategies[f"MODEL_{h}"] = ("slots", model_candidates(p, pred), h, None)
        print(f"model {h}h trained ({time.time() - started:.0f}s)", flush=True)
    results = {}
    for name, spec in strategies.items():
        results[name] = {}
        for period, (a, b) in PERIODS.items():
            s, e = p.index(a), p.index(b)
            curve, trades = (rotation(p, s, e, spec[1]) if spec[0] == "rotation"
                             else slots(p, s, e, spec[1], spec[2], spec[3]))
            results[name][period] = summary(curve, trades, p, s)
            results[name][period]["top_bases"] = top_bases(trades)
        d, ho = results[name]["dev"], results[name]["holdout"]
        print(f"{name:16} DEV x{d['btc_multiple']:.3f} dd {d['max_drawdown']:.0%} n{d['trades']:4d} | "
              f"HOLDOUT x{ho['btc_multiple']:.3f} dd {ho['max_drawdown']:.0%} n{ho['trades']:4d} "
              f"win {ho['win_rate'] or 0:.0%} mean {(ho['mean_trade'] or 0) * 100:+.2f}%", flush=True)
    candidates = [n for n in results if n != "LIVE" and results[n]["dev"]["trades"] >= 30]
    chosen = max(candidates, key=lambda n: results[n]["dev"]["btc_multiple"])
    ho, live = results[chosen]["holdout"], results["LIVE"]["holdout"]
    decision = {"chosen_on_dev": chosen, "holdout": ho, "live_holdout": live,
                "adopt": bool(ho["btc_multiple"] > 1 and ho["btc_multiple"] > live["btc_multiple"] and ho["max_drawdown"] <= 0.60)}
    print("DECISION", json.dumps({k: v for k, v in decision.items() if k != "holdout" and k != "live_holdout"}), flush=True)
    report = {"results": results, "decision": decision, "spread_fits": p.fits, "model_log": model_log,
              "minutes": round((time.time() - started) / 60, 1)}
    (output / "results.json").write_text(json.dumps(report, indent=1, default=float))
    return report


def top_bases(trades, n=5):
    from collections import Counter
    total = Counter()
    for x in trades:
        total[x["base"]] += x["ret"]
    return [(b, round(float(v), 3)) for b, v in total.most_common(n)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--threads", type=int, default=16)
    run(threads=parser.parse_args(argv).threads)


if __name__ == "__main__":
    main()
