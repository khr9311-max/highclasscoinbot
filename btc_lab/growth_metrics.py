"""Calendar returns and recovery time for a BTC-denominated equity curve.

The input is the full result of ``btc_lab.engine.run``. Curve timestamps denote
interval ends, except for the initial balance. A close at February 1 00:00 UTC
belongs to January, so monthly grouping uses ``t - 1``. No trading decision or
strategy selection belongs in this module.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import math
from numbers import Real


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _utc(timestamp: float) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Equity timestamp is outside the supported calendar") from exc


def _month_boundary(instant: datetime) -> bool:
    return (instant.day == 1 and instant.hour == 0 and instant.minute == 0
            and instant.second == 0 and instant.microsecond == 0)


def _return_pct(ending: float, starting: float) -> float | None:
    # A negative wallet is preserved as insolvency, but negative denominators
    # must not produce fictitious positive compound growth after bankruptcy.
    return (ending / starting - 1) * 100 if starting > 0 else None


def growth_metrics(result: Mapping) -> dict:
    """Measure calendar compounding and elapsed peak-to-recovery durations.

    Month dictionaries use ``YYYY-MM`` keys in chronological order. Monthly
    returns include partial first/last months; ``partial_months`` identifies
    them. Trailing twelve-month returns require observed UTC month-end anchors
    exactly twelve calendar months apart. A month-opening initial balance may
    serve as the preceding month-end anchor without creating a phantom monthly
    return. Positive fraction is 0..1, or ``None`` without eligible windows.

    Drawdown time starts at the latest preceding high, includes the recovery
    observation, and also counts unfinished episodes through the final point.
    Returning exactly to the peak counts as recovery. All times are elapsed
    calendar days, not numbers of samples. The function does not infer missing
    samples or use a later observation to fill a missing month-end.
    """
    if not isinstance(result, Mapping):
        raise ValueError("Result must be a mapping containing equity_curve")
    raw_curve = result.get("equity_curve")
    if not isinstance(raw_curve, Sequence) or isinstance(raw_curve, (str, bytes)) or not raw_curve:
        raise ValueError("A nonempty equity_curve is required")
    curve = []
    for point in raw_curve:
        if not isinstance(point, Mapping):
            raise ValueError("Equity points must be mappings")
        timestamp = _number(point.get("t"), "Equity timestamp")
        amount = _number(point.get("equity_btc"), "BTC equity")
        _utc(timestamp)
        if curve and timestamp <= curve[-1][0]:
            raise ValueError("Equity timestamps must be strictly increasing")
        curve.append((timestamp, amount))
    start_time, initial = curve[0]
    if initial <= 0:
        raise ValueError("Initial BTC equity must be positive")

    peak, peak_time = initial, start_time
    max_underwater = 0.0
    underwater = False
    recovered = 0
    max_drawdown = 0.0
    for timestamp, amount in curve[1:]:
        if amount >= peak:
            if underwater:
                max_underwater = max(max_underwater, timestamp - peak_time)
                recovered += 1
            peak, peak_time = amount, timestamp
            underwater = False
        else:
            underwater = True
            max_underwater = max(max_underwater, timestamp - peak_time)
            max_drawdown = max(max_drawdown, 1 - amount / peak)

    # Each exact boundary supplies the preceding calendar month's closing
    # equity. The initial point is an anchor only if it is such a boundary.
    anchors: dict[str, float] = {}
    monthly_equity: dict[str, float] = {}
    monthly_end_times: dict[str, float] = {}
    for index, (timestamp, amount) in enumerate(curve):
        instant = _utc(timestamp)
        month = _utc(timestamp - 1).strftime("%Y-%m")
        if _month_boundary(instant):
            anchors[month] = amount
        if index:
            monthly_equity[month] = amount
            monthly_end_times[month] = timestamp

    monthly_returns: dict[str, float | None] = {}
    monthly_compounded: dict[str, float] = {}
    partial_months: list[str] = []
    previous = initial
    for month, amount in monthly_equity.items():
        monthly_returns[month] = _return_pct(amount, previous)
        monthly_compounded[month] = (amount / initial - 1) * 100
        previous = amount
        month_start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc).timestamp()
        if start_time > month_start or month not in anchors:
            partial_months.append(month)

    rolling: dict[str, float] = {}
    for month in monthly_equity:
        if month not in anchors:
            continue
        year, month_number = map(int, month.split("-"))
        previous_year_month = f"{year - 1:04d}-{month_number:02d}"
        if previous_year_month not in anchors:
            continue
        value = _return_pct(anchors[month], anchors[previous_year_month])
        if value is not None:
            rolling[month] = value

    terminal_time, terminal = curve[-1]
    return {
        "initial_equity_btc": initial,
        "terminal_equity_btc": terminal,
        "cumulative_return_pct": (terminal / initial - 1) * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "max_underwater_days": max_underwater / 86_400,
        "current_underwater_days": (terminal_time - peak_time) / 86_400 if underwater else 0.0,
        "current_drawdown_pct": (1 - terminal / peak) * 100,
        "recovered_drawdown_episodes": recovered,
        "monthly_returns_pct": monthly_returns,
        "monthly_equity_btc": monthly_equity,
        "monthly_compounded_return_pct": monthly_compounded,
        "monthly_end_times": monthly_end_times,
        "partial_months": partial_months,
        "complete_month_ends": [month for month in monthly_equity if month in anchors],
        "rolling_12m_return_pct": rolling,
        "rolling_12m_positive_fraction": sum(value > 0 for value in rolling.values()) / len(rolling) if rolling else None,
        "worst_12m_return_pct": min(rolling.values()) if rolling else None,
        "nonpositive_equity_observed": any(amount <= 0 for _, amount in curve),
    }
