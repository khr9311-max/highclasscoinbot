"""
성과 지표 · DSR · PBO · 워크포워드 · 기간 안정성.

DSR / PBO 계산식은 업비트 봇 cross_validation.BacktestMetrics 의 '수정된' 구현을
그대로 옮겼다 (왜도·첨도 보정 DSR, rankdata 로 순위를 구하는 CSCV PBO).
입력은 전부 바이낸스 COIN-M 백테스트 결과다 - 업비트 결과를 쓰지 않는다.

수익률 단위:
  - 거래별 수익률 ret = 순손익(BTC) / 진입 시점 equity(BTC)   (위험 기반 사이징이라 ≈ R x 0.5%)
  - 일간 수익률 = 봉 마감 equity 곡선(BTC, 미실현 포함)의 하루 변화율
  - Sharpe/Sortino 는 일간 수익률로 연율화(x sqrt(365)), 거래당 Sharpe 는 연율화하지 않는다
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import scipy.stats as stats


# ---------------------------------------------------------------------------
# DSR / PBO (cross_validation.BacktestMetrics 에서 옮김)
# ---------------------------------------------------------------------------
def deflated_sharpe(returns: Sequence[float], num_trials: int, variance_of_trials: float) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = r.size
    if T < 3 or num_trials < 2 or variance_of_trials <= 0:
        return float("nan")
    sd = r.std(ddof=1)
    if sd <= 0:
        return float("nan")
    sr = r.mean() / sd
    g3 = float(stats.skew(r, bias=False))
    g4 = float(stats.kurtosis(r, fisher=False, bias=False))
    em = 0.5772156649
    sr_scale = math.sqrt(variance_of_trials)
    max_sr = sr_scale * ((1 - em) * stats.norm.ppf(1 - 1.0 / num_trials)
                         + em * stats.norm.ppf(1 - 1.0 / (num_trials * math.e)))
    denom_sq = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr ** 2
    if denom_sq <= 0:
        denom_sq = 1.0
    z = (sr - max_sr) * math.sqrt(T - 1) / math.sqrt(denom_sq)
    return float(stats.norm.cdf(z))


def pbo_cscv(matrix: np.ndarray, n_splits: int = 16, mc_sims: int = 300,
             rng: Optional[np.random.Generator] = None) -> float:
    mat = np.asarray(matrix, dtype=float)
    if mat.ndim != 2 or mat.shape[0] < 2 or n_splits % 2:
        return float("nan")
    N, T = mat.shape
    if T < n_splits * 2:
        return float("nan")
    rng = rng or np.random.default_rng(7)
    size = T // n_splits
    half = n_splits // 2
    over = 0
    for _ in range(mc_sims):
        perm = rng.permutation(n_splits)
        is_idx, oos_idx = perm[:half], perm[half:]
        is_ret = np.hstack([mat[:, k * size:(k + 1) * size] for k in is_idx])
        oos_ret = np.hstack([mat[:, k * size:(k + 1) * size] for k in oos_idx])
        is_sr = is_ret.mean(axis=1) / (is_ret.std(axis=1) + 1e-12)
        oos_sr = oos_ret.mean(axis=1) / (oos_ret.std(axis=1) + 1e-12)
        best = int(np.argmax(is_sr))
        rank = stats.rankdata(oos_sr)[best] - 1.0
        if rank < (N - 1) / 2.0:
            over += 1
    return over / mc_sims


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------
def max_drawdown(curve: Sequence[float]) -> Tuple[float, float]:
    """(최대 낙폭 비율, 최대 낙폭 BTC)."""
    x = np.asarray(curve, dtype=float)
    if x.size == 0:
        return 0.0, 0.0
    peak = np.maximum.accumulate(x)
    dd = (peak - x)
    rel = dd / np.where(peak > 0, peak, np.nan)
    return float(np.nanmax(rel)) if rel.size else 0.0, float(dd.max())


def daily_returns(curve: Sequence[float], ts: Sequence[float]) -> Tuple[np.ndarray, np.ndarray]:
    """봉 마감 equity -> UTC 일별 마지막 값 -> 일간 수익률."""
    c = np.asarray(curve, dtype=float)
    t = np.asarray(ts, dtype=float)
    if c.size < 2:
        return np.zeros(0), np.zeros(0)
    days = (t // 86400).astype(np.int64)
    last_idx = np.r_[np.nonzero(np.diff(days))[0], len(days) - 1]
    eod = c[last_idx]
    r = eod[1:] / eod[:-1] - 1.0
    return r, days[last_idx][1:]


def sharpe_sortino(daily: np.ndarray) -> Tuple[Optional[float], Optional[float]]:
    if daily.size < 10:
        return None, None
    mu, sd = float(daily.mean()), float(daily.std(ddof=1))
    down = daily[daily < 0]
    dsd = float(np.sqrt((down ** 2).mean())) if down.size else 0.0
    sh = mu / sd * math.sqrt(365) if sd > 0 else None
    so = mu / dsd * math.sqrt(365) if dsd > 0 else None
    return sh, so


def summarize_trades(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"n": 0}
    r = np.asarray([t["ret"] for t in trades], dtype=float)
    net = np.asarray([t["net_btc"] for t in trades], dtype=float)
    gross = np.asarray([t.get("realized_btc", t["net_btc"]) / t["equity_at_entry"]
                        if t.get("equity_at_entry") else 0.0 for t in trades], dtype=float)
    rr = np.asarray([t["r"] for t in trades if t.get("r") is not None], dtype=float)
    gains, losses = net[net > 0].sum(), -net[net < 0].sum()
    sd = float(r.std(ddof=1)) if r.size > 1 else 0.0
    return {
        "n": int(r.size),
        "win_rate": float((net > 0).mean()),
        "avg_return": float(r.mean()),
        "avg_gross_return": float(gross.mean()),
        "median_return": float(np.median(r)),
        "total_net_btc": float(net.sum()),
        "total_net_usd": float(sum(t["net_usd"] for t in trades)),
        "profit_factor": float(gains / losses) if losses > 0 else None,
        "expectancy_btc": float(net.mean()),
        "expectancy_r": float(rr.mean()) if rr.size else None,
        "median_r": float(np.median(rr)) if rr.size else None,
        "sharpe_per_trade": float(r.mean() / sd) if sd > 0 else None,
        "best_return": float(r.max()),
        "worst_return": float(r.min()),
        "avg_bars_held": float(np.mean([t["bars_held"] for t in trades])),
        "fees_btc": float(sum(t["fee_btc"] for t in trades)),
        "funding_btc": float(sum(t["funding_btc"] for t in trades)),
    }


def summarize_run(res: Any) -> Dict[str, Any]:
    s = summarize_trades(res.trades)
    mdd, mdd_btc = max_drawdown(res.equity_curve)
    daily, _ = daily_returns(res.equity_curve, res.equity_ts)
    sh, so = sharpe_sortino(daily)
    s.update({
        "start_equity_btc": res.config.start_equity_btc,
        "final_equity_btc": float(res.final_equity),
        "total_return": float(res.final_equity / res.config.start_equity_btc - 1.0),
        "max_drawdown": mdd, "max_drawdown_btc": mdd_btc,
        "sharpe_daily_ann": sh, "sortino_daily_ann": so,
        "skipped": len(res.skipped),
        "skip_reasons": _count([x.get("skip_reason", "").split(" (")[0] for x in res.skipped]),
        "exit_reasons": _count([t["exit_reason"] for t in res.trades]),
    })
    return s


def _count(xs: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def by_direction(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {"long": summarize_trades([t for t in trades if t["direction"] > 0]),
            "short": summarize_trades([t for t in trades if t["direction"] < 0])}


def period_stability(trades: Sequence[Dict[str, Any]], freq: str = "year") -> Dict[str, Any]:
    import datetime as dt
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        d = dt.datetime.fromtimestamp(t["signal_ts"], dt.timezone.utc)
        key = str(d.year) if freq == "year" else f"{d.year}H{1 if d.month <= 6 else 2}"
        buckets.setdefault(key, []).append(t)
    rows = {}
    for k in sorted(buckets):
        s = summarize_trades(buckets[k])
        rows[k] = {"n": s["n"], "avg_return": s["avg_return"], "win_rate": s["win_rate"],
                   "total_net_btc": s["total_net_btc"], "profit_factor": s["profit_factor"]}
    pos = sum(1 for v in rows.values() if v["total_net_btc"] > 0)
    return {"periods": rows, "positive_periods": pos, "total_periods": len(rows),
            "positive_ratio": pos / len(rows) if rows else None}


def top_trade_dependence(trades: Sequence[Dict[str, Any]], k: int = 5) -> Dict[str, Any]:
    """상위 k 건을 빼면 평균이 어떻게 되는가 (소수 대박 의존도)."""
    if len(trades) <= k:
        return {"k": k, "avg_without_top": None}
    r = sorted((t["ret"] for t in trades), reverse=True)
    rest = r[k:]
    return {"k": k, "avg_without_top": float(np.mean(rest)),
            "share_of_total_from_top": float(sum(r[:k]) / sum(r)) if sum(r) != 0 else None}


# ---------------------------------------------------------------------------
# 시행 집합 평가 (DSR / PBO)
# ---------------------------------------------------------------------------
def daily_matrix(runs: Dict[str, Any]) -> Tuple[Optional[np.ndarray], List[str]]:
    names = list(runs)
    series = {}
    all_days = set()
    for name, res in runs.items():
        r, days = daily_returns(res.equity_curve, res.equity_ts)
        series[name] = dict(zip(days.tolist(), r.tolist()))
        all_days |= set(days.tolist())
    if len(all_days) < 64:
        return None, names
    days = sorted(all_days)
    mat = np.zeros((len(names), len(days)))
    for row, name in enumerate(names):
        m = series[name]
        mat[row] = [m.get(d, 0.0) for d in days]
    return mat, names


def trial_statistics(runs: Dict[str, Any], chosen: str, min_trades: int = 10) -> Dict[str, Any]:
    srs = []
    for name, res in runs.items():
        r = [t["ret"] for t in res.trades]
        if len(r) >= min_trades and np.std(r, ddof=1) > 0:
            srs.append(float(np.mean(r) / np.std(r, ddof=1)))
    chosen_r = [t["ret"] for t in runs[chosen].trades]
    dsr = None
    var = float(np.var(srs, ddof=1)) if len(srs) >= 2 else 0.0
    if len(chosen_r) >= 10 and var > 0:
        dsr = deflated_sharpe(chosen_r, num_trials=len(runs), variance_of_trials=var)
    mat, names = daily_matrix(runs)
    pbo = pbo_cscv(mat, n_splits=16, mc_sims=500) if mat is not None else None
    return {"num_trials": len(runs), "dsr": dsr, "pbo": pbo, "trial_sr_variance": var,
            "trials_with_min_trades": len(srs)}


# ---------------------------------------------------------------------------
# 워크포워드
# ---------------------------------------------------------------------------
def walk_forward(runs: Dict[str, Any], chosen: str, t_start: float, t_end: float,
                 folds: int = 6, min_is_trades: int = 10) -> Dict[str, Any]:
    """
    기간을 folds 개로 나눠 앵커드 워크포워드:
      k 번째 구간(OOS)마다, 그 앞 전체(IS)에서 거래당 Sharpe 가 가장 높은 변형을 골라
      k 구간 성과를 기록한다. 규칙은 고정이라(파라미터 적합 없음) '선택' 만 검증한다.
    사전에 정한 기본 전략(chosen)의 구간별 성과도 같이 낸다.
    """
    edges = np.linspace(t_start, t_end, folds + 1)

    def seg(trades, a, b):
        return [t for t in trades if a <= t["signal_ts"] < b]

    out = []
    for k in range(1, folds):
        is_a, is_b, oos_a, oos_b = edges[0], edges[k], edges[k], edges[k + 1]
        best, best_sr = None, -1e9
        for name, res in runs.items():
            r = [t["ret"] for t in seg(res.trades, is_a, is_b)]
            if len(r) < min_is_trades or np.std(r, ddof=1) <= 0:
                continue
            sr = float(np.mean(r) / np.std(r, ddof=1))
            if sr > best_sr:
                best, best_sr = name, sr
        sel = seg(runs[best].trades, oos_a, oos_b) if best else []
        dflt = seg(runs[chosen].trades, oos_a, oos_b)
        out.append({
            "fold": k, "oos_start": float(oos_a), "oos_end": float(oos_b), "selected": best,
            "selected_is_sr": best_sr if best else None,
            "selected_oos": summarize_trades(sel), "default_oos": summarize_trades(dflt)})
    sel_rets = [x["selected_oos"].get("avg_return") for x in out if x["selected_oos"].get("n")]
    def_rets = [x["default_oos"].get("avg_return") for x in out if x["default_oos"].get("n")]
    def_all = [t["ret"] for x in out for t in seg(runs[chosen].trades, x["oos_start"], x["oos_end"])]
    return {
        "folds": out,
        "selected_positive_folds": sum(1 for r in sel_rets if r and r > 0),
        "default_positive_folds": sum(1 for r in def_rets if r and r > 0),
        "evaluated_folds": len(out),
        "default_oos_avg_return": float(np.mean(def_all)) if def_all else None,
        "default_oos_trades": len(def_all),
        "selection_matches_default": sum(1 for x in out if x["selected"] == chosen),
    }
