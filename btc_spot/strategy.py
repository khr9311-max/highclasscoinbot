"""Causal fixed daily 20/60/120 momentum, including neutral ties."""
from datetime import datetime, timezone
from decimal import Decimal

DAY_MS = 86_400_000


def signal(klines, server_time_ms):
    now = int(server_time_ms)
    today = now // DAY_MS * DAY_MS
    closed = []
    previous = None
    for row in klines:
        opening, closing = int(row[0]), int(row[6])
        if opening % DAY_MS or closing != opening + DAY_MS - 1:
            raise ValueError("Daily candles must use complete UTC intervals")
        if previous is not None and opening <= previous:
            raise ValueError("Duplicate or reversed daily candles")
        previous = opening
        if opening >= today:
            continue
        if closing >= now:
            continue
        prices = [Decimal(str(row[i])) for i in (1, 2, 3, 4)]
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise ValueError("Invalid daily price")
        o, h, low, close = prices
        if not low <= min(o, close) <= max(o, close) <= h:
            raise ValueError("Invalid daily OHLC range")
        closed.append((opening, close))
    # Match the fixed research warm-up, rather than evaluating a short sample.
    if len(closed) < 201:
        raise ValueError("201 completed daily candles are required")
    window = closed[-201:]
    if window[-1][0] != today - DAY_MS:
        raise ValueError("Latest completed daily candle is missing")
    if any(b[0] - a[0] != DAY_MS for a, b in zip(window, window[1:])):
        raise ValueError("Daily candle history has gaps")
    latest = window[-1][1]
    votes = [int(latest > window[-1-lag][1]) - int(latest < window[-1-lag][1])
             for lag in (20, 60, 120)]
    weight = (Decimal(3) + sum(votes)) / Decimal(6)
    return {"decision_id": datetime.fromtimestamp(today/1000, timezone.utc).date().isoformat(),
            "target_btc_fraction": str(weight), "lookbacks_days": [20, 60, 120],
            "votes": votes, "last_closed_day_utc": datetime.fromtimestamp(window[-1][0]/1000, timezone.utc).date().isoformat(),
            "last_close": str(latest), "decision_time_ms": today,
            "execution_policy": "latest_closed_signal_at_current_book_once_per_UTC_day"}
