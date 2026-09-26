"""Exploratory follow-up: the learned switcher with limit orders and realistic fills.

Written after regime_switch results were read, because the learned model beat
costs only under the optimistic maker assumption (every order fills at the
decision price). This replaces that assumption; it is not pre-registered.

Rules fixed before this module was run:
- Same target path as regime_switch (lgbm predictions, hold_path state machine).
- When the target differs from the actual position at a close, a limit order
  rests at that close. A buy fills only if a later bar trades at least one tick
  below it (low <= price - tick); a sell only if high >= price + tick. Fill at
  the limit price, maker fee 0.02%.
- An order not filled within PATIENCE bars: an entry from flat is re-posted at
  the current close; an exit or flip is sent as a taker order at that close
  (fee 0.05% + 3 bps + half tick), so positions never linger past the rule.
- A new target replaces the working order.
Funding is charged on the position held at the start of each bar.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from btc_lab import regime_switch as rs
from btc_lab import strategy_search as ss

OUTPUT = rs.OUTPUT
TICK = ss.TICK["BTCUSD_PERP"]
PATIENCE = (3, 1)
HORIZONS = (12, 24)


def simulate(close, high, low, funding, target_next, patience, maker_fee=rs.MAKER_FEE):
    """Bar returns, per-bar end positions and trades for a target path.

    target_next[i] is the position wanted from the close of bar i (decided there).
    """
    n = len(close)
    taker = ss.per_side_cost("BTCUSD_PERP", close, 1.0)
    r = np.zeros(n)
    pos_end = np.zeros(n)
    a = 0.0
    order = [target_next[0], close[0], 0] if target_next[0] != 0 else None   # [target, price, age]
    trades, open_trade = [], None
    stats = {"maker_fills": 0, "taker_exits": 0, "reposts": 0, "cancelled": 0}

    def trade_ret(side, entry, exit_, cost_in, cost_out):
        gross = (1 - entry / exit_) if side > 0 else (entry / exit_ - 1)
        return gross - cost_in - cost_out

    for i in range(1, n):
        prev = close[i - 1]
        a0 = a
        ri = -a0 * funding[i]
        filled = False
        if order is not None:
            tgt, price, age = order
            buy = tgt > a0
            if (buy and low[i] <= price - TICK) or (not buy and high[i] >= price + TICK):
                ri += a0 * (1 - prev / price) + tgt * (1 - price / close[i]) - abs(tgt - a0) * maker_fee
                if open_trade is not None:
                    side, e_bar, e_px, e_cost = open_trade
                    trades.append((e_bar, i, trade_ret(side, e_px, price, e_cost, maker_fee)))
                    open_trade = None
                open_trade = (np.sign(tgt), i, price, maker_fee) if tgt != 0 else None
                a = tgt
                order = None
                filled = True
                stats["maker_fills"] += 1
            else:
                order[2] = age + 1
        if not filled:
            ri += a0 * (1 - prev / close[i])
        want = target_next[i]
        if order is not None and order[0] != want:
            order = None
            stats["cancelled"] += 1
        if want != a and order is not None and order[2] >= patience:
            if a == 0:
                order = [want, close[i], 0]
                stats["reposts"] += 1
            else:
                ri -= abs(want - a) * taker[i]
                side, e_bar, e_px, e_cost = open_trade
                trades.append((e_bar, i, trade_ret(side, e_px, close[i], e_cost, taker[i])))
                open_trade = (np.sign(want), i, close[i], taker[i]) if want != 0 else None
                a = want
                order = None
                stats["taker_exits"] += 1
        elif want != a and order is None:
            order = [want, close[i], 0]
        elif want == a:
            order = None
        r[i] = ri
        pos_end[i] = a
    if trades:
        e, x, v = map(np.array, zip(*trades))
        tr = ss.Trades(e.astype(int), x.astype(int), v, 1)
    else:
        tr = ss.Trades(np.array([], int), np.array([], int), np.array([]), 1)
    return r, pos_end, tr, stats


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    fr = rs.Frame()
    close, high, low = fr.px["close"], fr.px["high"], fr.px["low"]
    cost_rt = float(2 * np.nanmedian(rs.per_side("taker", close)))
    rows = []
    for h in HORIZONS:
        pred = np.load(OUTPUT / f"lgbm_pred_{h}.npy")
        signal = np.where(pred > cost_rt, 1, np.where(pred < -cost_rt, -1, 0))
        pos = rs.hold_path(signal, h)
        target_next = np.r_[pos[1:], 0.0]             # position wanted from each close
        for patience in PATIENCE:
            r, pos_end, tr, stats = simulate(close, high, low, fr.funding, target_next, patience)
            row = {"name": f"lgbm_{h}_maker_p{patience}", "h_bars": h, "patience_bars": patience, "fills": stats}
            for label, period in (("walk", rs.WALK), ("holdout", rs.HOLDOUT)):
                row[label] = rs.period_metrics(fr, r, pos_end, tr, period)
            rows.append(row)
            w, ho = row["walk"], row["holdout"]
            print(f"{row['name']:20} walk {w['return']:+9.2%} (dd {w['max_dd']:.1%}, {w['trades']} tr, "
                  f"mean {w['mean_trade'] or 0:+.4%}) | holdout {ho['return']:+9.2%} (dd {ho['max_dd']:.1%}, "
                  f"{ho['trades']} tr, mean {ho['mean_trade'] or 0:+.4%}) | {stats}", flush=True)
    (OUTPUT / "maker_fill.json").write_text(json.dumps({"patience": PATIENCE, "rows": rows}, indent=1, default=float))


if __name__ == "__main__":
    main()
