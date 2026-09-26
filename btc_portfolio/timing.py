"""Execution timing for aggressive COIN-M trades from the situation ledger's 2h prediction.

A trade the aggressive rule must make (closing for a direction flip, or entering)
may wait until the model favours it: buy when the 2h prediction >= 0, sell when
<= 0. The wait starts at the close of the 4h bar that first called for the
direction and ends after timing_max_wait_seconds; then the trade goes ahead.
The start is stored in the ledger, so restarts and new 4h bars never push the
deadline back. A missing, stale, invalid or unreadable prediction never delays
a trade. Emergency closes, disaster stops, the kill switch, daily resizing and
alt rotation are not timed.

Evidence: btc_lab/TIMING_OVERLAY_2026-09-26.md (2h model, 4h window: +2.67bp
walk t 5.92, +1.30bp holdout t 3.17 per execution against immediate trading).
"""
import json
import math
from pathlib import Path

FOUR_HOURS_MS = 14_400_000
MAX_AGE_MS = 600_000


def read_prediction(path, now_ms):
    """The ledger's latest 2h prediction, or (None, reason) when it must not be used."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        bar, pred = int(data["bar_close_ms"]), float(data["pred_24"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, "prediction_unreadable"
    if not math.isfinite(pred):
        return None, "prediction_invalid"
    if not 0 <= now_ms - bar <= MAX_AGE_MS:
        return None, "prediction_stale"
    return pred, None


def decide(state, config, coin, side, now_ms):
    """(go, info) for a trade on `side` (+1 buy, -1 sell) toward coin["direction"]."""
    if not config.timing_prediction_path:
        return True, {"timing": "disabled"}
    wait = state.get("timing_wait")
    if not wait or wait.get("direction") != coin["direction"]:
        wait = {"direction": coin["direction"], "since_ms": int(coin["bar"]) + FOUR_HOURS_MS}
        state.put("timing_wait", wait)
    deadline = int(wait["since_ms"]) + config.timing_max_wait_seconds*1000
    info = {"side": side, "since_ms": wait["since_ms"], "deadline_ms": deadline}
    if now_ms >= deadline:
        return True, {**info, "timing": "deadline"}
    pred, problem = read_prediction(config.timing_prediction_path, now_ms)
    if pred is None:
        return True, {**info, "timing": problem}
    go = pred >= 0 if side > 0 else pred <= 0
    return go, {**info, "timing": "favourable" if go else "waiting", "pred_2h": pred}


def clear(state):
    if state.get("timing_wait") is not None:
        state.put("timing_wait", None)
