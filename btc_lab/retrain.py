"""Monthly self-update of the situation ledger's 1h/2h models behind a fixed gate.

Runs on the server from btc-ledger-retrain.timer (3rd of each month). It only
refits the LightGBM models that btc_lab.ledger evaluates and that
btc_portfolio.timing reads through prediction.json, the same way the research
validated them (monthly refit on all earlier data, regime_switch walk-forward).
Trading rules are never changed here.

1. Refresh public archives through the end of the last complete UTC month.
2. Probe: fit on data before that month (one-day purge) and predict the month
   on non-overlapping windows. Rank IC, and correlation with the live model.
3. Gate, fixed before use:
   - features unchanged and tree export parity exact,
   - archives complete for the window,
   - the candidate is trained on newer data than the live model,
   - probe 2h rank IC >= -0.05 (no clear breakdown),
   - probe 2h predictions correlate >= 0.3 with the live model (no gross drift).
   One month has only ~360 independent 2h windows, so the gate screens for
   broken data or pipelines; it does not select on recent performance.
4. On pass, fit on everything through the month end and replace the live model
   atomically (previous copy kept in history/). The ledger reloads it by mtime.
5. Report JSON and a Telegram summary.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from urllib.parse import urlencode

import numpy as np

from btc_lab import flow_data
from btc_lab import intraday_data as idata
from btc_lab import ledger as lg
from btc_lab import regime_switch as rs
from btc_lab import strategy_search as ss

FIRST_MONTH = (2021, 7)
START_MS = flow_data.START_MS
GATE_MIN_IC = -0.05
GATE_MIN_PRED_CORR = 0.3
BAR = lg.BAR


def ms(dt):
    return int(dt.timestamp() * 1000)


def window_for(now):
    """Train through the last complete UTC month; the probe month is that month."""
    end = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    last = (end.year - 1, 12) if end.month == 1 else (end.year, end.month - 1)
    probe = datetime(last[0], last[1], 1, tzinfo=timezone.utc)
    return {"end_ms": ms(end), "last_month": last, "probe_start_ms": ms(probe),
            "probe_month": f"{last[0]:04d}-{last[1]:02d}"}


def fetch_funding(start_ms, end_ms):
    rows, t = [], start_ms
    while t < end_ms:
        query = {"symbol": "BTCUSD_PERP", "startTime": t, "endTime": end_ms - 1, "limit": 1000}
        chunk = json.loads(idata.fetch("https://dapi.binance.com/dapi/v1/fundingRate?" + urlencode(query)))
        if not chunk:
            break
        rows += chunk
        t = int(chunk[-1]["fundingTime"]) + 1
        if len(chunk) < 1000:
            break
    return rows


def refresh(data_dir, w):
    """Archives only (complete months); returns (paths, problems)."""
    market, flow = data_dir / "market", data_dir / "flow"
    kl = idata.build("BTCUSD_PERP", output=market, first=FIRST_MONTH, last=w["last_month"], rest=None)
    window = flow_data.Window(start_ms=START_MS, end_ms=w["end_ms"], first_month=FIRST_MONTH,
                              last_month=w["last_month"], rest_start=None)
    manifest = flow_data.build_all(window, flow)
    funding = fetch_funding(START_MS - 86_400_000, w["end_ms"])
    with open(data_dir / "funding.csv", "w", newline="") as handle:
        out = csv.writer(handle)
        out.writerow(["funding_time_ms", "funding_rate", "mark_price"])
        for r in funding:
            out.writerow([r["fundingTime"], r["fundingRate"], r.get("markPrice", "")])
    problems = []
    if kl["missing_archive_months"] or kl["last"] + BAR != w["end_ms"]:
        problems.append("btcusd_perp_archive_incomplete")
    if any(v for k, v in manifest.items() if k.endswith("_missing_months")):
        problems.append("flow_archive_months_missing")
    probe_day = datetime.fromtimestamp(w["probe_start_ms"] / 1000, timezone.utc).date().isoformat()
    if any(d >= probe_day for d in manifest["metrics_missing_days"]):
        problems.append("metrics_days_missing_in_probe_month")
    if not funding or int(funding[-1]["fundingTime"]) < w["end_ms"] - 9 * 3_600_000:
        problems.append("funding_incomplete")
    paths = {"flow": flow / "flow_5m.npz", "market": market, "funding": data_dir / "funding.csv"}
    return paths, problems, {"klines": kl, "flow_missing_days": manifest["metrics_missing_days"],
                             "funding_rows": len(funding)}


def probe_stats(trees, live_trees, x, fwd, rows):
    pred = np.array([lg.predict_one(trees, [float(v) for v in x[i]]) for i in rows])
    live = np.array([lg.predict_one(live_trees, [float(v) for v in x[i]]) for i in rows])
    y = fwd[rows]
    return {"n": int(len(rows)), "rank_ic": ss.spearman(pred, y), "live_model_rank_ic_in_sample": ss.spearman(live, y),
            "pred_corr_with_live": float(np.corrcoef(pred, live)[0, 1]),
            "pred_std": float(pred.std()), "live_pred_std": float(live.std())}


def gate(current, train_end_ms, probe, problems):
    checks = {"features_unchanged": current["features"] == list(rs.LGBM_FEATURES),
              "archives_complete": not problems,
              "newer_than_live": train_end_ms > int(current["train_end_ms"]),
              "probe_2h_ic_not_broken": probe["24"]["rank_ic"] >= GATE_MIN_IC,
              "probe_2h_tracks_live_model": probe["24"]["pred_corr_with_live"] >= GATE_MIN_PRED_CORR}
    return checks, all(checks.values())


def promote(model_path, payload):
    """Keep the previous model in history/, then replace the live file atomically."""
    model_path = Path(model_path)
    history = model_path.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    if model_path.exists():
        previous = json.loads(model_path.read_text())
        shutil.copy2(model_path, history / f"ledger_model-{previous['train_end_ms']}.json")
    lg.write_model(model_path, payload)


def retrain(data_dir, model_path, now, dry_run=False):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    w = window_for(now)
    current = lg.load_model(model_path)
    paths, problems, data_info = refresh(data_dir, w)
    fr = rs.Frame(flow_path=paths["flow"], market_dir=paths["market"], funding_path=paths["funding"])
    x, close, cuts = lg.prepare_frame(fr)
    start = fr.index(rs.DEV[0]) + rs.DAY
    a = int((w["probe_start_ms"] - int(fr.open_ms[0])) // BAR)
    probe = {}
    for h in lg.MODEL_HORIZONS:
        trees, _ = lg.fit_horizon(x, close, h, start, a - rs.DAY)
        rows = np.arange(a, fr.n - h, h)
        probe[str(h)] = probe_stats(trees, current["models"][str(h)], x, rs.forward_return(close, h), rows)
    models, parity = {}, {}
    for h in lg.MODEL_HORIZONS:
        models[str(h)], parity[h] = lg.fit_horizon(x, close, h, start, fr.n)
    train_end_ms = int(fr.open_ms[-1]) + BAR
    checks, passed = gate(current, train_end_ms, probe, problems)
    report = {"run_utc": now.isoformat(timespec="seconds"), "dry_run": dry_run, "window": w,
              "current_train_end_ms": int(current["train_end_ms"]), "candidate_train_end_ms": train_end_ms,
              "probe": probe, "problems": problems, "checks": checks, "passed": passed,
              "promoted": passed and not dry_run, "data": data_info, "parity_max_abs": parity,
              "peak_rss_mb": peak_rss_mb()}
    if report["promoted"]:
        payload = lg.model_payload(models, close, cuts, train_end_ms, current.get("backtest_rank_ic_holdout", {}),
                                   parity, retrain={k: report[k] for k in ("window", "probe", "checks")})
        promote(model_path, payload)
    reports = data_dir / "reports"
    reports.mkdir(exist_ok=True)
    lg.write_model(reports / f"retrain-{w['probe_month']}{'-dry' if dry_run else ''}.json", report)
    return report


def peak_rss_mb():
    try:
        import resource
    except ImportError:                                  # Windows
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


def day(ms_):
    return datetime.fromtimestamp(ms_ / 1000, timezone.utc).strftime("%Y-%m-%d")


def message(report):
    p2, p1 = report["probe"]["24"], report["probe"]["12"]
    verdict = ("교체함" if report["promoted"] else
               "시험 실행: 교체하지 않음" if report["dry_run"] and report["passed"] else
               "유지함 (" + ", ".join(k for k, v in report["checks"].items() if not v) + ")")
    return "\n".join([
        f"상황 원장 모델 월간 재학습{' (시험 실행)' if report['dry_run'] else ''} · {report['run_utc'][:10]}",
        f"학습 자료: ~{day(report['candidate_train_end_ms'] - 1)} (현재 모델 ~{day(report['current_train_end_ms'] - 1)})",
        f"점검 {report['window']['probe_month']} (학습에서 뺀 달): 2시간 IC {p2['rank_ic']:+.3f} (n={p2['n']}),"
        f" 1시간 IC {p1['rank_ic']:+.3f} (n={p1['n']})",
        f"현재 모델과 예측 상관 {p2['pred_corr_with_live']:.2f}",
        f"판정: {verdict}",
        "매매 규칙은 바꾸지 않는다.",
    ])


async def notify(credentials_file, text):
    import aiohttp
    from btc_spot.notify import send_message, settings
    config = settings(credentials_file)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        await send_message(session, config, text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, help="Telegram settings (.env)")
    parser.add_argument("--dry-run", action="store_true", help="evaluate and report, never replace the model")
    parser.add_argument("--now", help="UTC date to act as today (YYYY-MM-DD), for tests")
    args = parser.parse_args(argv)
    from binance_coinm_v1.runtime.instance_lock import InstanceLock
    now = (datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now
           else datetime.now(timezone.utc))
    lock = InstanceLock(Path(args.data_dir) / "retrain.lock")
    lock.acquire()
    try:
        try:
            report = retrain(args.data_dir, args.model, now, args.dry_run)
        except Exception as exc:
            if args.credentials_file:
                asyncio.run(notify(args.credentials_file, f"상황 원장 월간 재학습 실패: {type(exc).__name__}. 기존 모델을 유지한다."))
            raise
        print(json.dumps({k: report[k] for k in ("checks", "passed", "promoted", "probe")}, default=float))
        if args.credentials_file:
            asyncio.run(notify(args.credentials_file, message(report)))
    finally:
        lock.release()


if __name__ == "__main__":
    main()
