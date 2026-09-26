"""Live BTCUSD_PERP situation ledger: exchange data, model predictions and automatic scoring.

Replaces the hand-kept hourly report ledger. After every 5-minute close it
rebuilds the regime_switch features from public Binance REST data, evaluates
the learned 1h/2h models, stores a snapshot in SQLite and later fills the
realized forward returns. Once an hour it can send a Korean summary to
Telegram. It uses no Binance credentials and never places orders.

Parity with the research data:
- Only completed 5m klines are used.
- A metrics API row stamped T equals the archive row with create_time T-5m
  (checked 2026-09-24), so API rows are placed at T-5m before features()
  applies its one-bar lag.
- Features are cast to float32 before the trees are evaluated, as in training.
The 2h model's edge is about half the round-trip cost (REGIME_SWITCH_2026-09-26),
so predictions are reported as execution-timing context, not entry signals.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

from btc_lab import llm_judge
from btc_lab import regime_switch as rs

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "btc_lab/state/ledger_model/ledger_model.json"
DEFAULT_STATE = ROOT / "btc_lab/state/ledger_pc"
BAR = 300_000
LOOKBACK = 1000
MODEL_HORIZONS = (12, 24)
API_ROUNDED = ("um_top_account_ls", "um_top_position_ls", "um_global_ls")
SCORE_HORIZONS = (12, 24, 48)
DELAY_MS = 20_000
KST = timezone(timedelta(hours=9))
DAPI = "https://dapi.binance.com"
FAPI = "https://fapi.binance.com"
ALLOWED = (DAPI, FAPI)
TREND = {-1: "하락", 0: "횡보", 1: "상승"}
VOL = {0: "저변동", 1: "고변동"}
FLOW = {-1: "매도우위", 0: "중립", 1: "매수우위"}
OI = {0: "OI감소", 1: "OI증가"}


# ---------------------------------------------------------------- model export and evaluation

def export_booster(booster):
    """Compact arrays per tree; child >= 0 is a node, child < 0 is leaf -child-1."""
    trees = []
    for info in booster.dump_model()["tree_info"]:
        t = {"f": [], "t": [], "dl": [], "mt": [], "l": [], "r": [], "leaf": []}

        def walk(n):
            if "split_index" not in n:
                t["leaf"].append(float(n["leaf_value"]))
                return -len(t["leaf"])
            if n["decision_type"] != "<=":
                raise ValueError("Only numerical splits are supported")
            k = len(t["f"])
            for key in ("f", "t", "dl", "mt", "l", "r"):
                t[key].append(None)
            t["f"][k], t["t"][k] = int(n["split_feature"]), float(n["threshold"])
            t["dl"][k], t["mt"][k] = bool(n["default_left"]), str(n["missing_type"])
            t["l"][k] = walk(n["left_child"])
            t["r"][k] = walk(n["right_child"])
            return k

        t["root"] = walk(info["tree_structure"])
        trees.append(t)
    return trees


def predict_one(trees, x):
    """LightGBM raw prediction for one row (numerical splits, NaN/None/Zero missing types)."""
    total = 0.0
    for t in trees:
        k = t["root"]
        while k >= 0:
            v = x[t["f"][k]]
            mt = t["mt"][k]
            if math.isnan(v) and mt != "NaN":
                v = 0.0
            if (mt == "NaN" and math.isnan(v)) or (mt == "Zero" and abs(v) <= 1e-35):
                left = t["dl"][k]
            else:
                left = v <= t["t"][k]
            k = t["l"][k] if left else t["r"][k]
        total += t["leaf"][-k - 1]
    return total


def model_row(f, i, names):
    return [float(np.float32(f[k][i])) for k in names]


def prepare_frame(fr):
    """Model inputs as the live API sees them: (x float32, close, situation thresholds)."""
    for k in API_ROUNDED:                                # the live API reports these ratios to 4 decimals
        fr.flow[k] = np.round(fr.flow[k], 4)
    f = rs.features(fr)
    dev_mask = np.zeros(fr.n, bool)
    dev_mask[fr.index(rs.DEV[0]):fr.index(rs.DEV[1])] = True
    _, cuts = rs.situation_cells(f, dev_mask)
    x = np.column_stack([f[k] for k in rs.LGBM_FEATURES]).astype(np.float32)
    return x, fr.px["close"], cuts


def fit_horizon(x, close, h, start, stop):
    """Fit on bars in [start, stop) whose h-bar labels end before stop; export and check parity."""
    import lightgbm as lgb
    fwd = rs.forward_return(close, h)
    idx = np.arange(start, stop - h, rs.TRAIN_STEP)
    idx = idx[~np.isnan(fwd[idx])]
    y = fwd[idx]
    lo, hi = np.percentile(y, [0.5, 99.5])
    model = lgb.LGBMRegressor(**rs.LGBM_PARAMS).fit(x[idx], np.clip(y, lo, hi))
    trees = export_booster(model.booster_)
    sample = x[max(start, stop - 3000):stop]
    mine = np.array([predict_one(trees, [float(v) for v in row]) for row in sample])
    parity = float(np.max(np.abs(model.predict(sample) - mine)))
    if parity > 1e-9:
        raise RuntimeError(f"Tree export mismatch for h={h}: {parity}")
    return trees, parity


def model_payload(models, close, cuts, train_end_ms, reference, parity, **extra):
    return {"features": list(rs.LGBM_FEATURES), "horizons": list(MODEL_HORIZONS), "models": models,
            "cost_round_trip": float(2 * np.nanmedian(rs.per_side("taker", close))),
            "cell_thresholds": cuts, "train_end_ms": int(train_end_ms),
            "lgbm_params": rs.LGBM_PARAMS, "backtest_rank_ic_holdout": reference, "parity_max_abs": parity,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra}


def train(output=MODEL_PATH):
    """Fit the 1h/2h models on all local history (PC), export them and check tree parity."""
    fr = rs.Frame()
    x, close, cuts = prepare_frame(fr)
    start = fr.index(rs.DEV[0]) + rs.DAY
    reference = {}
    results = rs.OUTPUT / "results.json"
    if results.is_file():
        diag = json.loads(results.read_text())["diagnostics"]["horizons"]
        reference = {h: diag[str(h)]["lgbm"]["rank_ic_holdout"] for h in MODEL_HORIZONS if str(h) in diag}
    models, parity = {}, {}
    for h in MODEL_HORIZONS:
        models[str(h)], parity[h] = fit_horizon(x, close, h, start, fr.n)
    payload = model_payload(models, close, cuts, int(fr.open_ms[-1]) + BAR, reference, parity)
    write_model(output, payload)
    return payload


def write_model(path, payload):
    """Replace the model file atomically; the running ledger reloads it by mtime."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, path)


def load_model(path=MODEL_PATH):
    model = json.loads(Path(path).read_text())
    if model["features"] != list(rs.LGBM_FEATURES):
        raise ValueError("Model features differ from regime_switch.LGBM_FEATURES")
    return model


# ---------------------------------------------------------------- live frame

class LiveFrame:
    """Same attributes as regime_switch.Frame, built from REST responses."""

    def __init__(self, raw, now_ms, lookback=LOOKBACK):
        done = [r for r in raw["cm"] if int(r[6]) < now_ms]
        if not done:
            raise ValueError("No completed COIN-M klines")
        last = int(done[-1][0])
        if last != (now_ms // BAR) * BAR - BAR:
            raise ValueError("Latest completed 5m kline is missing")
        self.open_ms = last - BAR * np.arange(lookback - 1, -1, -1, dtype=np.int64)
        self.n = lookback
        cols = {k: self.place([int(r[0]) for r in done], [float(r[c]) for r in done])
                for k, c in (("open", 1), ("high", 2), ("low", 3), ("close", 4), ("cm_volume", 5), ("cm_taker_buy", 9))}
        close = _ffill(cols["close"])
        if np.isnan(close).any():
            raise ValueError("Not enough COIN-M history")
        self.px = {"close": close}
        for k in ("open", "high", "low"):
            self.px[k] = np.where(np.isnan(cols[k]), close, cols[k])
        um = [r for r in raw["um"] if int(r[6]) < now_ms]
        prem = [r for r in raw["premium"] if int(r[6]) < now_ms]
        self.flow = {"cm_volume": cols["cm_volume"], "cm_taker_buy": cols["cm_taker_buy"],
                     "um_volume": self.place([int(r[0]) for r in um], [float(r[5]) for r in um]),
                     "um_taker_buy": self.place([int(r[0]) for r in um], [float(r[9]) for r in um]),
                     "premium_index": self.place([int(r[0]) for r in prem], [float(r[4]) for r in prem])}
        for key, field, name in (("oi", "sumOpenInterest", "um_oi"), ("top_account", "longShortRatio", "um_top_account_ls"),
                                 ("top_position", "longShortRatio", "um_top_position_ls"),
                                 ("global", "longShortRatio", "um_global_ls")):
            rows = raw[key]
            self.flow[name] = self.place([int(r["timestamp"]) - BAR for r in rows], [float(r[field]) for r in rows])
        self.funding = np.zeros(self.n)
        known = np.full(self.n, np.nan)
        before = None
        for r in sorted(raw["funding"], key=lambda r: int(r["fundingTime"])):
            i = (int(r["fundingTime"]) - int(self.open_ms[0])) // BAR
            if i < 0:
                before = float(r["fundingRate"])
            elif i < self.n:
                self.funding[i] += float(r["fundingRate"])
                known[i] = float(r["fundingRate"])
        if before is not None and np.isnan(known[0]):
            known[0] = before
        self.funding_known = _ffill(known)
        self.metric_latest_ms = max((int(r["timestamp"]) for r in raw["oi"]), default=0)

    def place(self, times, values):
        out = np.full(self.n, np.nan)
        idx = (np.asarray(times, dtype=np.int64) - int(self.open_ms[0])) // BAR
        ok = (idx >= 0) & (idx < self.n)
        out[idx[ok]] = np.asarray(values, dtype=float)[ok]
        return out


def _ffill(x):
    out = np.array(x, dtype=float)
    for i in range(1, len(out)):
        if np.isnan(out[i]):
            out[i] = out[i - 1]
    return out


REQUESTS = {
    "cm": (DAPI + "/dapi/v1/klines", {"symbol": "BTCUSD_PERP", "interval": "5m", "limit": LOOKBACK + 2}),
    "um": (FAPI + "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "5m", "limit": LOOKBACK + 2}),
    "premium": (DAPI + "/dapi/v1/premiumIndexKlines", {"symbol": "BTCUSD_PERP", "interval": "5m", "limit": LOOKBACK + 2}),
    "oi": (FAPI + "/futures/data/openInterestHist", {"symbol": "BTCUSDT", "period": "5m", "limit": 500}),
    "top_account": (FAPI + "/futures/data/topLongShortAccountRatio", {"symbol": "BTCUSDT", "period": "5m", "limit": 500}),
    "top_position": (FAPI + "/futures/data/topLongShortPositionRatio", {"symbol": "BTCUSDT", "period": "5m", "limit": 500}),
    "global": (FAPI + "/futures/data/globalLongShortAccountRatio", {"symbol": "BTCUSDT", "period": "5m", "limit": 500}),
    "funding": (DAPI + "/dapi/v1/fundingRate", {"symbol": "BTCUSD_PERP", "limit": 100}),
    "premium_now": (DAPI + "/dapi/v1/premiumIndex", {"symbol": "BTCUSD_PERP"}),
    "cm_oi_now": (DAPI + "/dapi/v1/openInterest", {"symbol": "BTCUSD_PERP"}),
    "k4h": (DAPI + "/dapi/v1/klines", {"symbol": "BTCUSD_PERP", "interval": "4h", "limit": 300}),
}
METRIC_KEYS = ("oi", "top_account", "top_position", "global")


class RateLimited(RuntimeError):
    pass


async def get_json(session, url, params):
    if not url.startswith(ALLOWED):
        raise ValueError("Only public Binance futures hosts are allowed")
    async with session.get(url, params=params, allow_redirects=False) as response:
        if response.status in (418, 429):
            raise RateLimited(f"HTTP {response.status}")
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return await response.json(content_type=None)


async def fetch(session, keys=None):
    keys = list(keys or REQUESTS)
    values = await asyncio.gather(*(get_json(session, *REQUESTS[k]) for k in keys))
    return dict(zip(keys, values))


# ---------------------------------------------------------------- snapshot

def atr(fr, k, n=14):
    """Average true range of completed k-bar candles aligned to UTC, in USD."""
    groups = (fr.open_ms // (k * BAR)).astype(np.int64)
    ends = np.flatnonzero(np.r_[groups[1:] != groups[:-1], True])
    if k > 1 and (int(fr.open_ms[-1]) + BAR) % (k * BAR):
        ends = ends[:-1]                               # last group not complete
    starts = np.r_[0, ends[:-1] + 1]
    h = np.array([fr.px["high"][a:b + 1].max() for a, b in zip(starts, ends)])
    low = np.array([fr.px["low"][a:b + 1].min() for a, b in zip(starts, ends)])
    c = fr.px["close"][ends]
    tr = np.maximum(h[1:] - low[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(low[1:] - c[:-1])))
    return float(tr[-n:].mean()) if len(tr) >= n else None


def swing_signal(rows, now_ms):
    """btc_portfolio's COIN-M rule: EMA20/80 of the last 200 completed 4h closes."""
    done = [float(r[4]) for r in rows if int(r[6]) < now_ms][-200:]
    if len(done) < 200:
        return None

    def ema(values, span):
        a, out = 2 / (span + 1), values[0]
        for v in values[1:]:
            out += a * (v - out)
        return out
    fast, slow = ema(done, 20), ema(done, 80)
    return {"direction": 1 if fast > slow else -1 if fast < slow else 0, "gap": fast / slow - 1}


def cell_label(f, i, cuts):
    tz, fl, vr, d = f["trend_z"][i], f["um_flow_3"][i], f["vol_ratio"][i], f["doi_12"][i]
    if any(np.isnan(v) for v in (tz, fl, vr, d)):
        return None
    trend = -1 if tz < cuts["trend_z"][0] else 1 if tz > cuts["trend_z"][1] else 0
    flow = -1 if fl < cuts["um_flow_3"][0] else 1 if fl > cuts["um_flow_3"][1] else 0
    vol, oi = int(vr > cuts["vol_ratio"]), int(d > 0)
    return {"id": int((trend + 1) * 12 + vol * 6 + (flow + 1) * 2 + oi),
            "text": f"{TREND[trend]}·{VOL[vol]}·{FLOW[flow]}·{OI[oi]}"}


def _num(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def _one(value):
    return value[0] if isinstance(value, list) else value


def snapshot(fr, raw, model, now_ms):
    f = rs.features(fr)
    i = fr.n - 1
    x = model_row(f, i, model["features"])
    preds = {h: predict_one(model["models"][str(h)], x) for h in model["horizons"]}
    close = fr.px["close"]
    day = fr.open_ms // 86_400_000
    today = day == day[-1]
    first = int(np.flatnonzero(today)[0])
    vol = np.nan_to_num(fr.flow["cm_volume"][today])
    typical = ((fr.px["high"] + fr.px["low"] + close) / 3)[today]
    vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() > 0 else None
    prem = _one(raw["premium_now"])
    mark, index = float(prem["markPrice"]), float(prem["indexPrice"])
    oi_rows = sorted(raw["oi"], key=lambda r: int(r["timestamp"]))
    oi = [float(r["sumOpenInterest"]) for r in oi_rows]

    def doi(k):
        return oi[-1] / oi[-1 - k] - 1 if len(oi) > k else None

    def latest(key):
        return sorted(raw[key], key=lambda r: int(r["timestamp"]))

    top_pos, top_acc, glob = latest("top_position"), latest("top_account"), latest("global")
    funding = sorted(raw["funding"], key=lambda r: int(r["fundingTime"]))
    cm_oi = float(_one(raw["cm_oi_now"])["openInterest"])
    return {
        "bar_close_ms": int(fr.open_ms[i]) + BAR, "created_ms": int(now_ms),
        "close": float(close[i]), "mark": mark, "index": index, "premium": mark / index - 1,
        "daily_open": float(fr.px["open"][first]), "vwap": vwap,
        "cm_oi_contracts": cm_oi, "cm_oi_btc": cm_oi * 100 / mark,
        "um_oi_btc": oi[-1] if oi else None, "um_doi": {str(k): _num(doi(k)) for k in (1, 3, 12, 48)},
        "taker_bs": {v: {str(k): _num(f[f"{v}_bs_{k}"][i]) for k in (1, 3, 12)} for v in ("um", "cm")},
        "top_position_ls": float(top_pos[-1]["longShortRatio"]) if top_pos else None,
        "top_position_long": float(top_pos[-1]["longAccount"]) if top_pos else None,
        "top_position_ls_d1h": (float(top_pos[-1]["longShortRatio"]) - float(top_pos[-13]["longShortRatio"])
                                if len(top_pos) > 12 else None),
        "top_account_ls": float(top_acc[-1]["longShortRatio"]) if top_acc else None,
        "global_ls": float(glob[-1]["longShortRatio"]) if glob else None,
        "funding_last": float(funding[-1]["fundingRate"]) if funding else None,
        "funding_next_est": float(prem["lastFundingRate"]), "funding_next_ms": int(prem["nextFundingTime"]),
        "atr": {"5m": atr(fr, 1), "15m": atr(fr, 3), "1h": atr(fr, 12)},
        "situation": cell_label(f, i, model["cell_thresholds"]),
        "pred": {str(h): v for h, v in preds.items()},
        "cost_round_trip": model["cost_round_trip"],
        "swing": swing_signal(raw["k4h"], now_ms),
        "metrics_fresh": fr.metric_latest_ms >= int(fr.open_ms[i]),
        "model_inputs": {k: _num(v) for k, v in zip(model["features"], x)},
    }


# ---------------------------------------------------------------- storage and scoring

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  bar_close_ms INTEGER PRIMARY KEY, created_ms INTEGER NOT NULL, close REAL NOT NULL,
  pred_12 REAL, pred_24 REAL, fwd_12 REAL, fwd_24 REAL, fwd_48 REAL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports(hour_ms INTEGER PRIMARY KEY, sent_ms INTEGER NOT NULL, message_id INTEGER);
"""


def open_db(state_dir):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state_dir / "ledger.sqlite3")
    db.executescript(SCHEMA + llm_judge.SCHEMA_SQL)
    return db


def write_prediction(state_dir, snap, model):
    """Latest predictions for btc_portfolio.timing, replaced atomically."""
    path = Path(state_dir) / "prediction.json"
    tmp = path.with_name("prediction.json.tmp")
    tmp.write_text(json.dumps({"bar_close_ms": snap["bar_close_ms"], "created_ms": snap["created_ms"],
                               "pred_12": snap["pred"].get("12"), "pred_24": snap["pred"].get("24"),
                               "model_train_end_ms": model.get("train_end_ms")}))
    os.replace(tmp, path)


def store(db, snap):
    db.execute("INSERT OR IGNORE INTO snapshots(bar_close_ms, created_ms, close, pred_12, pred_24, payload) "
               "VALUES(?,?,?,?,?,?)",
               (snap["bar_close_ms"], snap["created_ms"], snap["close"], snap["pred"].get("12"),
                snap["pred"].get("24"), json.dumps(snap, separators=(",", ":"))))
    db.commit()


def score(db, fr):
    """Fill inverse forward returns close-to-close for snapshots whose horizon has passed."""
    first_close = int(fr.open_ms[0]) + BAR
    closes = fr.px["close"]
    filled = 0
    for h in SCORE_HORIZONS:
        rows = db.execute(f"SELECT bar_close_ms, close FROM snapshots WHERE fwd_{h} IS NULL AND bar_close_ms >= ?",
                          (first_close,)).fetchall()
        for t, c in rows:
            j = (t + h * BAR - first_close) // BAR
            if j < fr.n:
                db.execute(f"UPDATE snapshots SET fwd_{h}=? WHERE bar_close_ms=?", (1 - c / closes[j], t))
                filled += 1
    db.commit()
    return filled


def score_stats(db, since_ms, h):
    rows = db.execute(f"SELECT pred_{h}, fwd_{h} FROM snapshots WHERE bar_close_ms >= ? AND pred_{h} IS NOT NULL "
                      f"AND fwd_{h} IS NOT NULL", (since_ms,)).fetchall()
    if len(rows) < 30:
        return {"n": len(rows)}
    p, y = np.array(rows).T
    rank = lambda v: np.argsort(np.argsort(v))
    return {"n": len(rows), "hit": float(np.mean(np.sign(p) == np.sign(y))),
            "ic": float(np.corrcoef(rank(p), rank(y))[0, 1])}


# ---------------------------------------------------------------- report

def _pct(v, digits=2):
    return "-" if v is None else f"{v * 100:+.{digits}f}%"


def _ratio(v):
    return "-" if v is None else f"{v:.2f}"


def report_text(snap, stats=None, backtest_ic=None):
    t = datetime.fromtimestamp(snap["bar_close_ms"] / 1000, KST)
    nxt = datetime.fromtimestamp(snap["funding_next_ms"] / 1000, KST)
    um, cm = snap["taker_bs"]["um"], snap["taker_bs"]["cm"]
    atr_ = snap["atr"]
    cost = snap["cost_round_trip"]
    p1, p2 = snap["pred"].get("12"), snap["pred"].get("24")
    side = "매수 체결에 유리" if p2 is not None and p2 >= 0 else "매도 체결에 유리"
    d1h = snap["top_position_ls_d1h"]
    d1h_text = "-" if d1h is None else f"{d1h:+.3f}"
    lines = [
        f"BTCUSD_PERP 상황 원장 · {t:%Y-%m-%d %H:%M} KST (거래소 실측)",
        f"가격  Mark {snap['mark']:,.1f} · Index {snap['index']:,.1f} · 프리미엄 {_pct(snap['premium'], 3)}",
        f"기준  일봉 시가(09시) {snap['daily_open']:,.1f} ({_pct(snap['close'] / snap['daily_open'] - 1)})"
        + (f" · VWAP {snap['vwap']:,.1f} ({_pct(snap['close'] / snap['vwap'] - 1)})" if snap["vwap"] else ""),
        f"테이커 B/S  USDT-M 5m {_ratio(um['1'])} · 15m {_ratio(um['3'])} · 1h {_ratio(um['12'])}"
        f" | COIN-M 1h {_ratio(cm['12'])}",
        f"OI  COIN-M {snap['cm_oi_contracts'] / 1e6:.2f}M계약({snap['cm_oi_btc']:,.0f} BTC) · USDT-M "
        f"{(snap['um_oi_btc'] or 0):,.0f} BTC (15m {_pct(snap['um_doi']['3'])}, 1h {_pct(snap['um_doi']['12'])},"
        f" 4h {_pct(snap['um_doi']['48'])})",
        f"포지션(USDT-M)  상위 트레이더 롱 {(snap['top_position_long'] or 0) * 100:.1f}% (L/S {_ratio(snap['top_position_ls'])},"
        f" 1h {d1h_text})"
        f" · 전체 계정 L/S {_ratio(snap['global_ls'])}",
        f"펀딩  직전 {_pct(snap['funding_last'], 4)} · 예상 {_pct(snap['funding_next_est'], 4)} (다음 {nxt:%H:%M} KST)",
        "ATR  " + " · ".join(f"{k} ${v:,.0f}" if v else f"{k} -" for k, v in atr_.items()),
        f"상황  {snap['situation']['text'] if snap['situation'] else '자료 부족'}",
        f"모델  1시간 {_pct(p1, 3)} · 2시간 {_pct(p2, 3)} → {side}"
        + (" (비용 초과: 드묾)" if p2 is not None and abs(p2) > cost else f" (왕복 비용 {cost * 100:.2f}% 미만: 단독 진입 근거 아님)"),
    ]
    if snap["swing"]:
        lines.append(f"swing COIN-M  4h EMA20/80 {'롱' if snap['swing']['direction'] > 0 else '숏'}"
                     f" (격차 {_pct(snap['swing']['gap'])})")
    if stats:
        parts = []
        for label, s in stats.items():
            if s.get("n", 0) >= 30:
                parts.append(f"{label} 적중 {s['hit'] * 100:.0f}% IC {s['ic']:+.3f} (n={s['n']})")
            else:
                parts.append(f"{label} 표본 부족 (n={s.get('n', 0)})")
        lines.append("채점(2시간 예측)  " + " · ".join(parts)
                     + (f" | 백테스트 IC {backtest_ic:+.3f}" if backtest_ic is not None else ""))
    if not snap["metrics_fresh"]:
        lines.append("주의: USDT-M 포지셔닝 통계가 최신 봉보다 늦게 도착했다.")
    return "\n".join(lines)


def stats_for(db, now_ms):
    return {"7일": score_stats(db, now_ms - 7 * 86_400_000, 24), "30일": score_stats(db, now_ms - 30 * 86_400_000, 24)}


# ---------------------------------------------------------------- beginner report

def read_portfolio(path, now_ms):
    """Read-only view of the trading bot's status.json; None when it cannot be read."""
    try:
        s = json.loads(Path(path).read_text(encoding="utf-8"))
        r = s.get("result") or {}
        alt = (r.get("alt_signal") or {}).get("symbol")
        held = bool(alt) and float((s.get("wallet") or {}).get(alt[:-3], 0)) > 0
        return {"equity_btc": float(r["equity_btc"]), "coin_qty": float(s.get("coin_qty") or 0),
                "alt": alt[:-3] if held else None, "halt": s.get("halt"),
                "waiting": bool(r.get("coinm_timing_wait")), "age_s": (now_ms - int(s["updated_at_ms"])) / 1000}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mood(taker_bs_1h):
    if taker_bs_1h is None:
        return "알 수 없음"
    if taker_bs_1h > 1.10:
        return "사려는 쪽이 강함"
    if taker_bs_1h > 1.03:
        return "사려는 쪽이 조금 우세"
    if taker_bs_1h < 0.90:
        return "팔려는 쪽이 강함"
    if taker_bs_1h < 0.97:
        return "팔려는 쪽이 조금 우세"
    return "팽팽함"


def _earlier(db, snap, ms):
    row = db.execute("SELECT payload FROM snapshots WHERE bar_close_ms=?", (snap["bar_close_ms"] - ms,)).fetchone()
    return json.loads(row[0]) if row else None


def simple_report(db, snap, krw_rate=1390.0):
    """A few plain lines for the hourly Telegram message; details stay in the ledger and CSV."""
    t = datetime.fromtimestamp(snap["bar_close_ms"] / 1000, KST)
    lines = [f"[비트코인 봇] {t:%m/%d %H:%M}"]
    p, price = snap.get("portfolio"), snap["mark"]
    if p:
        day = _earlier(db, snap, 86_400_000)
        change = ""
        if day and day.get("portfolio"):
            change = f" · 하루 {_pct(p['equity_btc'] / day['portfolio']['equity_btc'] - 1, 1)}"
        lines.append(f"💰 내 자산 {p['equity_btc']:.6f} BTC (약 {p['equity_btc'] * price * krw_rate / 10000:.1f}만원){change}")
    hour = _earlier(db, snap, 3_600_000)
    one_h = f" · 1시간 {_pct(snap['close'] / hour['close'] - 1, 1)}" if hour else ""
    lines.append(f"📊 비트코인 {price:,.0f}달러{one_h} · 오늘 {_pct(snap['close'] / snap['daily_open'] - 1, 1)}")
    if p:
        if p["halt"]:
            lines.append(f"⚠️ 봇 멈춤: {p['halt']}")
        elif p["age_s"] > 300:
            lines.append("⚠️ 봇 상태가 5분 넘게 갱신되지 않음")
        else:
            fut = ("상승에 베팅 중(선물 롱)" if p["coin_qty"] > 0 else "하락에 베팅 중(선물 숏)" if p["coin_qty"] < 0
                   else "선물 포지션 없음")
            alt = f" + 알트 {p['alt']} 보유" if p["alt"] else " + 알트 없음(BTC로 대기)"
            lines.append("🤖 봇: " + fut + alt + (" · 방향 바꿀 타이밍 기다리는 중" if p["waiting"] else ""))
    lines.append("🌡️ 분위기: " + mood(snap["taker_bs"]["um"].get("12")))
    lines += llm_judge.simple_lines(db, snap["bar_close_ms"])
    return "\n".join(lines)


# ---------------------------------------------------------------- service

async def cycle(session, db, model, now_ms, portfolio_path=None):
    raw = await fetch(session)
    last_open = (now_ms // BAR) * BAR - BAR
    for _ in range(3):                                   # metrics rows can arrive a little late
        if max((int(r["timestamp"]) for r in raw["oi"]), default=0) >= last_open:
            break
        await asyncio.sleep(15)
        raw.update(await fetch(session, METRIC_KEYS))
    fr = LiveFrame(raw, now_ms)
    snap = snapshot(fr, raw, model, now_ms)
    snap["portfolio"] = read_portfolio(portfolio_path, now_ms) if portfolio_path else None
    store(db, snap)
    score(db, fr)
    return snap, fr


async def judge(session, db, cfg, snap, fr):
    """Record Gemini's shadow call for this bar; failures are stored, never raised."""
    from btc_spot.runtime import safe_error
    try:
        decision = await llm_judge.ask(session, cfg, llm_judge.build_prompt(snap, fr.px["close"]))
        llm_judge.store(db, snap["bar_close_ms"], cfg["model"], decision)
    except Exception as exc:
        print(json.dumps({"gemini": safe_error(exc)}), flush=True)
        llm_judge.store(db, snap["bar_close_ms"], cfg["model"], error=type(exc).__name__)


async def run(args):
    import aiohttp
    from binance_coinm_v1.runtime.instance_lock import InstanceLock
    from btc_spot.runtime import safe_error
    model = load_model(args.model)
    db = open_db(args.state_dir)
    lock = InstanceLock(Path(args.state_dir) / "ledger.lock")
    lock.acquire()
    config = None
    if args.credentials_file:
        from btc_spot.notify import settings
        config = settings(args.credentials_file)
    gemini = llm_judge.settings(args.gemini_file) if args.gemini_file else None
    model_mtime = Path(args.model).stat().st_mtime_ns
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            while True:
                now_ms = int(time.time() * 1000)
                try:
                    mtime = Path(args.model).stat().st_mtime_ns
                    if mtime != model_mtime:
                        model, model_mtime = load_model(args.model), mtime
                        print(json.dumps({"model_reloaded_train_end_ms": model["train_end_ms"]}), flush=True)
                except Exception as exc:                 # keep the loaded model on a bad file
                    print(json.dumps(safe_error(exc)), flush=True)
                try:
                    snap, fr = await cycle(session, db, model, now_ms, args.portfolio_status)
                    write_prediction(args.state_dir, snap, model)
                    if gemini and (llm_judge.due(snap["bar_close_ms"]) or args.once):
                        await judge(session, db, gemini, snap, fr)
                    status = {"updated_ms": now_ms, "bar_close_ms": snap["bar_close_ms"],
                              "metrics_fresh": snap["metrics_fresh"]}
                    (Path(args.state_dir) / "status.json").write_text(json.dumps(status))
                    if snap["bar_close_ms"] % 3_600_000 == 0:
                        await hourly(session, db, model, snap, config, args.once)
                except RateLimited as exc:
                    print(json.dumps(safe_error(exc)), flush=True)
                    await asyncio.sleep(120)
                except Exception as exc:
                    print(json.dumps(safe_error(exc)), flush=True)
                if args.once:
                    return
                now = time.time() * 1000
                await asyncio.sleep(max(1.0, ((now // BAR + 1) * BAR + DELAY_MS - now) / 1000))
    finally:
        db.close()
        lock.release()


async def hourly(session, db, model, snap, config, printed):
    hour = snap["bar_close_ms"]
    if db.execute("SELECT 1 FROM reports WHERE hour_ms=?", (hour,)).fetchone():
        return
    text = simple_report(db, snap, config.fallback_krw if config else 1390.0)
    if config is None:
        if not printed:
            print(text, flush=True)
        return
    from btc_spot.notify import send_message
    message_id = await send_message(session, config, text)
    db.execute("INSERT OR IGNORE INTO reports VALUES(?,?,?)", (hour, int(time.time() * 1000), message_id))
    db.commit()


def export(state_dir, out):
    db = open_db(state_dir)
    rows = db.execute("SELECT payload, fwd_12, fwd_24, fwd_48 FROM snapshots ORDER BY bar_close_ms").fetchall()
    db.close()
    fields = ["time_kst", "close", "mark", "index", "premium", "daily_open", "vwap", "cm_oi_contracts", "um_oi_btc",
              "um_doi_15m", "um_doi_1h", "um_doi_4h", "taker_um_5m", "taker_um_15m", "taker_um_1h", "taker_cm_1h",
              "top_position_long", "top_position_ls", "top_position_ls_d1h", "global_ls", "funding_last",
              "funding_next_est", "atr_5m", "atr_15m", "atr_1h", "situation", "pred_1h", "pred_2h", "swing",
              "realized_1h", "realized_2h", "realized_4h"]
    with open(out, "w", newline="", encoding="utf-8-sig") as handle:
        w = csv.writer(handle)
        w.writerow(fields)
        for payload, f12, f24, f48 in rows:
            s = json.loads(payload)
            w.writerow([datetime.fromtimestamp(s["bar_close_ms"] / 1000, KST).strftime("%Y-%m-%d %H:%M"), s["close"],
                        s["mark"], s["index"], s["premium"], s["daily_open"], s["vwap"], s["cm_oi_contracts"],
                        s["um_oi_btc"], s["um_doi"]["3"], s["um_doi"]["12"], s["um_doi"]["48"],
                        s["taker_bs"]["um"]["1"], s["taker_bs"]["um"]["3"], s["taker_bs"]["um"]["12"],
                        s["taker_bs"]["cm"]["12"], s["top_position_long"], s["top_position_ls"],
                        s["top_position_ls_d1h"], s["global_ls"], s["funding_last"], s["funding_next_est"],
                        s["atr"]["5m"], s["atr"]["15m"], s["atr"]["1h"],
                        s["situation"]["text"] if s["situation"] else "", s["pred"].get("12"), s["pred"].get("24"),
                        (s["swing"] or {}).get("direction"), f12, f24, f48])
    return len(rows)


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("train", help="fit and export the 1h/2h models from local history (PC)")
    for name in ("once", "run"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        p.add_argument("--model", type=Path, default=MODEL_PATH)
        p.add_argument("--credentials-file", type=Path, help="Telegram settings (.env); omit to print only")
        p.add_argument("--gemini-file", type=Path, help="GEMINI_API_KEY [+ GEMINI_MODEL] (.env); omit to disable")
        p.add_argument("--portfolio-status", type=Path, help="trading bot status.json, read only for the report")
    p = sub.add_parser("export")
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "train":
        payload = train()
        print(json.dumps({k: payload[k] for k in ("train_end_ms", "cost_round_trip", "parity_max_abs",
                                                  "backtest_rank_ic_holdout")}))
    elif args.command == "export":
        print(json.dumps({"rows": export(args.state_dir, args.out), "out": str(args.out)}))
    else:
        args.once = args.command == "once"
        if args.once:
            args.credentials_file = None
        try:
            asyncio.run(run(args))
        except KeyboardInterrupt:                        # systemd stops the service with SIGINT
            return
        if args.once:
            db = open_db(args.state_dir)
            row = db.execute("SELECT payload FROM snapshots ORDER BY bar_close_ms DESC LIMIT 1").fetchone()
            if row:
                snap = json.loads(row[0])
                lines = [report_text(snap, stats_for(db, snap["bar_close_ms"])),
                         *llm_judge.report_lines(db, snap["bar_close_ms"])]
                print("\n".join(lines))
            db.close()


if __name__ == "__main__":
    main()
