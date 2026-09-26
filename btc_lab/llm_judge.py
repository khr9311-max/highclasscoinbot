"""Gemini as a shadow discretionary trader on the situation ledger (no orders).

Every JUDGE_MINUTES the ledger sends Gemini its exchange-sourced snapshot plus
the last 48 hourly and 18 four-hour BTCUSD_PERP returns, and records Gemini's
long/short/flat call for the next 1h and 4h. Calls are scored against the
realized inverse returns already stored with each snapshot, next to two
baselines on the same timestamps: always long, and the live swing direction
(4h EMA20/80). Only a result that beats both would justify discussing orders.

Gemini is told to use only the numbers given; the hourly Gemini reports the
user received earlier contained invented figures (see LEDGER.md).
"""
from __future__ import annotations

import json
from pathlib import Path
import time

import numpy as np

JUDGE_MINUTES = 15
DEFAULT_MODEL = "gemini-3.6-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DIRECTIONS = ("long", "short", "flat")
SYSTEM = (
    "You are a discretionary BTC futures trader. Decide the BTCUSD perpetual position for the next 1 hour and "
    "the next 4 hours from the JSON market snapshot you are given. Use only the numbers in the snapshot; never "
    "assume or invent other data. Returns are percent changes of the BTC price. 'flat' means no position. "
    "Answer with a single JSON object and nothing else: "
    '{"direction_1h": "long"|"short"|"flat", "direction_4h": "long"|"short"|"flat", '
    '"confidence": number between 0 and 1, "reason": "one short sentence in Korean, at most 80 characters"}'
)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_decisions(
  bar_close_ms INTEGER PRIMARY KEY, created_ms INTEGER NOT NULL, model TEXT NOT NULL,
  direction_1h TEXT, direction_4h TEXT, confidence REAL, reason TEXT, error TEXT);
"""


def settings(path):
    """GEMINI_API_KEY (required) and GEMINI_MODEL (optional) from a dedicated env file, or None."""
    from dotenv import dotenv_values
    path = Path(path)
    if not path.is_file():
        return None
    values = dotenv_values(path, interpolate=False)
    key = values.get("GEMINI_API_KEY")
    if not key:
        return None
    return {"key": key, "model": values.get("GEMINI_MODEL") or DEFAULT_MODEL}


def due(bar_close_ms):
    return bar_close_ms % (JUDGE_MINUTES * 60_000) == 0


def recent_returns(close, bars):
    """Percent returns of consecutive `bars`-sized steps ending at the last close, oldest first."""
    pts = close[::-1][::bars][::-1]
    return [round(float(v), 3) for v in (pts[1:] / pts[:-1] - 1) * 100]


def build_prompt(snap, close):
    keep = ("close", "mark", "index", "premium", "daily_open", "vwap", "cm_oi_contracts", "um_oi_btc", "um_doi",
            "taker_bs", "top_position_long", "top_position_ls", "top_position_ls_d1h", "global_ls",
            "funding_last", "funding_next_est", "atr", "situation", "swing")
    payload = {k: snap[k] for k in keep if k in snap}
    payload["time_utc"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(snap["bar_close_ms"] / 1000))
    payload["hourly_returns_pct_last_48h"] = recent_returns(np.asarray(close[-12 * 48 - 1:]), 12)
    payload["four_hour_returns_pct_last_3d"] = recent_returns(np.asarray(close[-48 * 18 - 1:]), 48)
    return json.dumps(payload, separators=(",", ":"), default=float)


def parse(text):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Gemini answer is not an object")
    d1, d4 = data.get("direction_1h"), data.get("direction_4h")
    conf = float(data.get("confidence"))
    if d1 not in DIRECTIONS or d4 not in DIRECTIONS or not 0 <= conf <= 1:
        raise ValueError("Gemini answer outside the schema")
    return {"direction_1h": d1, "direction_4h": d4, "confidence": conf, "reason": str(data.get("reason", ""))[:200]}


async def ask(session, cfg, prompt):
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "maxOutputTokens": 8192}}
    import aiohttp
    async with session.post(ENDPOINT.format(model=cfg["model"]), json=body, headers={"x-goog-api-key": cfg["key"]},
                            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=90)) as response:
        if response.status != 200:
            raise RuntimeError(f"Gemini HTTP {response.status}")      # never echo the body: it may quote the request
        data = await response.json(content_type=None)
    candidate = data["candidates"][0]
    finish = candidate.get("finishReason", "STOP")
    if finish != "STOP":                                   # thinking counts toward maxOutputTokens
        raise RuntimeError(f"Gemini finish {finish}")
    text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought")).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return parse(text)


def store(db, bar_close_ms, model, decision=None, error=None):
    db.execute("INSERT OR IGNORE INTO llm_decisions VALUES(?,?,?,?,?,?,?,?)",
               (bar_close_ms, int(time.time() * 1000), model,
                (decision or {}).get("direction_1h"), (decision or {}).get("direction_4h"),
                (decision or {}).get("confidence"), (decision or {}).get("reason"), error))
    db.commit()


SIGN = {"long": 1, "short": -1, "flat": 0}


def scores(db, since_ms):
    """Hit rates of Gemini vs always-long vs the swing direction on the same timestamps."""
    rows = db.execute("SELECT d.direction_1h, d.direction_4h, s.fwd_12, s.fwd_48, s.payload FROM llm_decisions d "
                      "JOIN snapshots s ON s.bar_close_ms = d.bar_close_ms WHERE d.bar_close_ms >= ? AND d.error IS NULL",
                      (since_ms,)).fetchall()
    out = {}
    for label, di, fi in (("1h", 0, 2), ("4h", 1, 3)):
        done = [r for r in rows if r[fi] is not None]
        calls = [(SIGN[r[di]], r[fi], (json.loads(r[4]).get("swing") or {}).get("direction", 0)) for r in done]
        taken = [(s, f, w) for s, f, w in calls if s != 0]
        hit = lambda pairs: float(np.mean([np.sign(f) == s for s, f in pairs])) if pairs else None
        out[label] = {"n": len(calls), "taken": len(taken), "hit": hit([(s, f) for s, f, _ in taken]),
                      "avg_move_pct": float(np.mean([s * f for s, f, _ in taken]) * 100) if taken else None,
                      "always_long_hit": hit([(1, f) for _, f, _ in calls]),
                      "swing_hit": hit([(w, f) for _, f, w in calls if w])}
    return out


def simple_lines(db, snap_ms, min_calls=20):
    """Gemini's latest 4h call and its 7-day hit rate next to 'always up', in plain words."""
    row = db.execute("SELECT direction_4h, error FROM llm_decisions ORDER BY bar_close_ms DESC LIMIT 1").fetchone()
    if not row:
        return []
    if row[1]:
        return ["🧪 AI 연습 판단: 이번에는 응답 실패 (매매엔 영향 없음)"]
    word = {"long": "오를 것", "short": "내릴 것", "flat": "애매함(관망)"}
    lines = [f"🧪 AI 연습 판단: 4시간 뒤 '{word[row[0]]}' (실제 매매엔 안 씀)"]
    s = scores(db, snap_ms - 7 * 86_400_000)["4h"]
    if s["taken"] < min_calls or s["always_long_hit"] is None:
        lines.append(f"   AI 성적: 채점 중 ({s['taken']}/{min_calls}건)")
    else:
        lines.append(f"   최근 7일 AI 적중 {s['hit'] * 100:.0f}% · 그냥 '오른다'고 찍었으면 {s['always_long_hit'] * 100:.0f}%")
    return lines


def report_lines(db, snap_ms):
    row = db.execute("SELECT direction_1h, direction_4h, confidence, reason, error FROM llm_decisions "
                     "ORDER BY bar_close_ms DESC LIMIT 1").fetchone()
    if not row:
        return []
    ko = {"long": "롱", "short": "숏", "flat": "관망"}
    if row[4]:
        lines = [f"Gemini 판단 (섀도, 주문 없음): 실패 ({row[4]})"]
    else:
        lines = [f"Gemini 판단 (섀도, 주문 없음): 1시간 {ko[row[0]]} · 4시간 {ko[row[1]]} (확신 {row[2]:.2f}) {row[3]}"]
    s = scores(db, snap_ms - 7 * 86_400_000)
    pct = lambda v: "-" if v is None else f"{v * 100:.0f}%"
    parts = [f"{h} 적중 {pct(v['hit'])} (거래 {v['taken']}/{v['n']}) · 항상 롱 {pct(v['always_long_hit'])} · swing {pct(v['swing_hit'])}"
             for h, v in s.items()]
    lines.append("Gemini 채점 7일: " + " | ".join(parts))
    return lines
