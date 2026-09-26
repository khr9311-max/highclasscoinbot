"""Pre-registered historical test of Gemini models as a shadow trader, after their training data.

Fixed on 2026-09-26 before any call:
- Window: hourly decisions from 2026-08-01 00:00 to 2026-09-24 20:00 UTC. Gemini
  3.6 Flash was last updated in July 2026; the Flash-Lite models are older.
- Prompt: the live one (llm_judge.build_prompt) - relative numbers only, no dates,
  price or open-interest levels. Snapshots are rebuilt from the research archives
  with the live rules: completed bars only, USDT-M positioning one bar late, L/S
  ratios at the API's 4 decimals, the swing EMA over the last 200 completed 4h closes.
- Scoring: 1h and 4h hit rates of non-flat calls, flat share and mean signed move,
  next to always-long and the swing direction on the same timestamps.
- Pass: 4h hit rate at least 3 points above both baselines and a higher mean signed
  4h move than the swing direction. If no model passes, the live judge is switched off.
Responses are cached per model and timestamp, so an interrupted run resumes without
paying twice. The API key is read from the given .env and never written or printed.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np

from btc_lab import ledger as lg
from btc_lab import llm_judge as lj
from btc_lab import regime_switch as rs

OUTPUT = rs.ROOT / "btc_lab/state/llm_backtest_20260926"
WINDOW = ("2026-08-01", "2026-09-24 20:00")                   # last decision whose 4h outcome is in the data
MODELS = {"gemini-3.6-flash": (0.75, 3.75, 4), "gemini-3.5-flash-lite": (0.30, 2.50, 8),
          "gemini-3.1-flash-lite": (0.25, 1.50, 8)}          # $/1M in, $/1M out (incl. thinking), concurrency
PASS_POINTS = 0.03


def snapshots():
    """(bar_close_ms, snap, closes up to that bar, fwd 1h, fwd 4h) for each hourly decision."""
    fr = rs.Frame()
    for k in lg.API_ROUNDED:
        fr.flow[k] = np.round(fr.flow[k], 4)
    f = rs.features(fr)
    model = lg.load_model()
    c, h, low, o = fr.px["close"], fr.px["high"], fr.px["low"], fr.px["open"]
    fwd1, fwd4 = rs.forward_return(c, 12), rs.forward_return(c, 48)
    four = 14_400_000
    k4 = np.flatnonzero((fr.open_ms + lg.BAR) % four == 0)          # 5m bars that close a 4h bar
    rows4 = [[int(fr.open_ms[j]) + lg.BAR - four, 0, 0, 0, float(c[j]), 0, int(fr.open_ms[j]) + lg.BAR - 1] for j in k4]
    close_times = [r[6] for r in rows4]
    day = fr.open_ms // 86_400_000
    vol = np.nan_to_num(fr.flow["cm_volume"])
    oi, top, glob = fr.flow["um_oi"], fr.flow["um_top_position_ls"], fr.flow["um_global_ls"]
    start = rs.stamp(WINDOW[0])
    end = rs.stamp(WINDOW[1])
    out = []
    for i in np.flatnonzero((fr.open_ms + lg.BAR) % 3_600_000 == 0):
        t = int(fr.open_ms[i]) + lg.BAR
        if not start <= t <= end:
            continue
        today = np.flatnonzero(day[: i + 1] == day[i])
        first = int(today[0])
        typical = (h[first:i + 1] + low[first:i + 1] + c[first:i + 1]) / 3
        v = vol[first:i + 1]
        window = SimpleNamespace(open_ms=fr.open_ms[i - 999:i + 1],
                                 px={"high": h[i - 999:i + 1], "low": low[i - 999:i + 1], "close": c[i - 999:i + 1]})
        m = i - 1                                                 # API positioning rows are one bar late
        ls = lambda arr, back=0: None if not np.isfinite(arr[m - back]) else float(arr[m - back])
        tp, tp1 = ls(top), ls(top, 12)
        swing = lg.swing_signal(rows4[: bisect.bisect_left(close_times, t)], t)
        snap = {"bar_close_ms": t, "close": float(c[i]), "daily_open": float(o[first]),
                "vwap": float((typical * v).sum() / v.sum()) if v.sum() > 0 else None,
                "premium": float(fr.flow["premium_index"][i]) if np.isfinite(fr.flow["premium_index"][i]) else None,
                "um_doi": {str(k): (float(oi[m] / oi[m - k] - 1) if np.isfinite(oi[m]) and np.isfinite(oi[m - k]) else None)
                           for k in (1, 3, 12, 48)},
                "taker_bs": {vv: {str(k): lg._num(f[f"{vv}_bs_{k}"][i]) for k in (1, 3, 12)} for vv in ("um", "cm")},
                "top_position_ls": tp, "top_position_long": tp / (1 + tp) if tp else None,
                "top_position_ls_d1h": tp - tp1 if tp is not None and tp1 is not None else None,
                "global_ls": ls(glob), "funding_last": lg._num(fr.funding_known[i]),
                "atr": {"5m": lg.atr(window, 1), "15m": lg.atr(window, 3), "1h": lg.atr(window, 12)},
                "situation": lg.cell_label(f, i, model["cell_thresholds"]), "swing": swing}
        out.append((t, snap, c[: i + 1], float(fwd1[i]), float(fwd4[i])))
    return out


async def run_model(session, name, key, items, cache_path):
    pin, pout, concurrency = MODELS[name]
    done = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done[row["t"]] = row
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    cfg = {"key": key, "model": name}

    async def one(t, snap, closes):
        if t in done and not done[t].get("error"):
            return
        async with sem:
            row = {"t": t, "model": name}
            for attempt in range(5):
                try:
                    row["decision"] = await lj.ask(session, cfg, lj.build_prompt(snap, closes))
                    row.pop("error", None)
                    break
                except Exception as exc:                  # 429 and transient errors back off; others recorded
                    row["error"] = type(exc).__name__ + (": " + str(exc)[:60] if isinstance(exc, RuntimeError) else "")
                    await asyncio.sleep(2 * (attempt + 1) * (3 if "429" in row["error"] else 1))
        async with lock:
            done[t] = row
            with cache_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    await asyncio.gather(*(one(t, s, cl) for t, s, cl, _, _ in items))
    return done


def score(items, rows):
    sign = {"long": 1, "short": -1, "flat": 0}
    out = {}
    for label, di in (("1h", 3), ("4h", 4)):
        key = "direction_" + label
        calls = [(sign[rows[t]["decision"][key]], it[di], (it[1]["swing"] or {}).get("direction", 0))
                 for it in items for t in [it[0]] if t in rows and "decision" in rows[t] and math.isfinite(it[di])]
        taken = [(s, f) for s, f, _ in calls if s]
        hit = lambda pairs: float(np.mean([np.sign(f) == s for s, f in pairs])) if pairs else None
        out[label] = {"calls": len(calls), "taken": len(taken), "flat_share": 1 - len(taken) / max(len(calls), 1),
                      "hit": hit(taken), "mean_signed_move_bp": float(np.mean([s * f for s, f in taken]) * 1e4) if taken else None,
                      "always_long_hit": hit([(1, f) for _, f, _ in calls]),
                      "always_long_move_bp": float(np.mean([f for _, f, _ in calls]) * 1e4) if calls else None,
                      "swing_hit": hit([(w, f) for _, f, w in calls if w]),
                      "swing_move_bp": float(np.mean([w * f for _, f, w in calls if w]) * 1e4) if calls else None}
    s4 = out["4h"]
    out["passes"] = bool(s4["hit"] is not None and s4["hit"] >= max(s4["always_long_hit"], s4["swing_hit"]) + PASS_POINTS
                         and s4["mean_signed_move_bp"] > s4["swing_move_bp"])
    return out


async def main_async(env_path, models):
    import aiohttp
    from dotenv import dotenv_values
    key = dotenv_values(env_path, interpolate=False).get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY missing in " + str(env_path))
    items = snapshots()
    print(f"{len(items)} hourly decisions {time.strftime('%Y-%m-%d %H:%M', time.gmtime(items[0][0] / 1000))} .. "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(items[-1][0] / 1000))} UTC", flush=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"window": WINDOW, "decisions": len(items), "models": {}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for name in models:
            started = time.time()
            rows = await run_model(session, name, key, items, OUTPUT / f"{name}.jsonl")
            errors = sum(1 for r in rows.values() if "decision" not in r)
            report["models"][name] = {**score(items, rows), "errors": errors, "minutes": round((time.time() - started) / 60, 1)}
            r4, r1 = report["models"][name]["4h"], report["models"][name]["1h"]
            fmt = lambda v: "-" if v is None else f"{v * 100:.1f}%"
            print(f"{name:24} 4h hit {fmt(r4['hit'])} (taken {r4['taken']}/{r4['calls']}, move {r4['mean_signed_move_bp']:+.1f}bp) "
                  f"vs always-long {fmt(r4['always_long_hit'])} ({r4['always_long_move_bp']:+.1f}bp) "
                  f"swing {fmt(r4['swing_hit'])} ({r4['swing_move_bp']:+.1f}bp) | 1h hit {fmt(r1['hit'])} "
                  f"vs {fmt(r1['always_long_hit'])}/{fmt(r1['swing_hit'])} | errors {errors} | PASS {report['models'][name]['passes']}",
                  flush=True)
    (OUTPUT / "results.json").write_text(json.dumps(report, indent=1))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", type=Path, default=rs.ROOT / ".env", help="file holding GEMINI_API_KEY")
    parser.add_argument("--models", nargs="*", default=list(MODELS))
    args = parser.parse_args(argv)
    asyncio.run(main_async(args.env, args.models))


if __name__ == "__main__":
    main()
