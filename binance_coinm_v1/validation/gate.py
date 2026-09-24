"""
바이낸스 COIN-M 전용 검증 게이트. 기본값은 닫힘.

업비트 봇의 검증 결과(state/naked_validation.json 등)는 읽지 않는다. 이 게이트가 보는
것은 이 패키지의 DB(validation_reports)에 저장된, 바이낸스 COIN-M 데이터로 만든 리포트뿐이다.

열리는 조건 (전부):
  1. 백테스트 표본        : 기본 전략 거래 VALIDATION_MIN_TRADES 건 이상
  2. 비용 후 기대값       : 거래당 평균 순수익 > 0 이고 BTC 기대값 > 0
  3. 낙폭                 : 최대낙폭 <= VALIDATION_MAX_DD_PCT
  4. DSR                  : >= VALIDATION_MIN_DSR (시행 수 = 비교한 조합 전부)
  5. PBO                  : <= VALIDATION_MAX_PBO
  6. 표본 외(워크포워드)  : 기본 전략의 OOS 평균 > 0, 양수 구간이 절반 이상
  7. 종이 매매 표본       : 백테스트 데이터 '이후' 신호의 종이 거래 VALIDATION_MIN_PAPER_TRADES 건 이상
  8. 종이 매매 기대값     : 그 평균 순수익 > 0
  9. 설정 지문            : 리포트 지문 == 현재 설정·계약 지문 (설정을 바꾸면 다시 검증)
 10. 신선도               : 리포트가 VALIDATION_MAX_AGE_DAYS 이내
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..config.settings import STRATEGY_VERSION, Settings

SOURCE = "binance_coinm_v1"
REPORT_VERSION = 2


def paper_trade_returns(db: Any, after_ts: float, symbol: str,
                        fingerprint: Optional[str] = None) -> List[Dict[str, Any]]:
    """백테스트 데이터 끝 이후 신호로 생긴 종이 거래 (체결·종료된 것, 채택된 고아 제외)."""
    out = []
    if not fingerprint or _number(after_ts) is None:
        return out
    for t in db.closed_positions("paper"):
        if t.get("symbol") != symbol or t.get("adopted") or not t.get("entry_avg_price"):
            continue
        if t.get("validation_fingerprint") != fingerprint or t.get("market_environment") != "live":
            continue
        sig = t.get("signal") or {}
        if (_number(sig.get("close_time")) or 0) <= after_ts:
            continue
        eq = _number(t.get("equity_at_entry_btc")) or 0.0
        acc = t.get("accounting") or {}
        net = _number(acc.get("net_pnl_btc"))
        if eq <= 0 or net is None or acc.get("accounting_complete") is not True:
            continue
        out.append({"trade_id": t["trade_id"], "net_btc": net,
                    "ret": net / eq, "fingerprint": fingerprint, "market_environment": "live",
                    "direction": t.get("direction"), "closed_at": t.get("closed_at")})
    return out


def build_report(settings: Settings, backtest: Dict[str, Any], paper: Sequence[Dict[str, Any]],
                 contract_essentials: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    s = backtest.get("chosen_summary") or {}
    tr = backtest.get("trial") or {}
    wf = backtest.get("walk_forward") or {}
    fp_now = settings.fingerprint(contract_essentials)
    paper = [p for p in paper if p.get("fingerprint") == fp_now
             and p.get("market_environment") == "live"
             and _number(p.get("ret")) is not None and _number(p.get("net_btc")) is not None]
    paper_n = len(paper)
    paper_mean = float(np.mean([p["ret"] for p in paper])) if paper else None
    dd = _number(s.get("max_drawdown"))
    dsr, pbo = _number(tr.get("dsr")), _number(tr.get("pbo"))
    folds = _number(wf.get("evaluated_folds")) or 0
    checks = {
        f"백테스트 거래 {settings.validation_min_trades}건 이상":
            (_number(s.get("n")) or 0) >= settings.validation_min_trades,
        "비용 후 기대값 > 0":
            (_number(s.get("avg_return")) or 0) > 0 and (_number(s.get("expectancy_btc")) or 0) > 0,
        f"최대낙폭 <= {settings.validation_max_dd_pct:g}%":
            dd is not None and 0 <= dd * 100 <= settings.validation_max_dd_pct,
        f"DSR >= {settings.validation_min_dsr:g}":
            dsr is not None and settings.validation_min_dsr <= dsr <= 1,
        f"PBO <= {settings.validation_max_pbo:g}":
            pbo is not None and 0 <= pbo <= settings.validation_max_pbo,
        "워크포워드 OOS 평균 > 0 & 양수 구간 절반 이상":
            (_number(wf.get("default_oos_avg_return")) or 0) > 0 and folds > 0 and
            math.ceil(folds / 2) <= (_number(wf.get("default_positive_folds")) or 0) <= folds,
        f"표본 외 종이매매 {settings.validation_min_paper_trades}건 이상":
            paper_n >= settings.validation_min_paper_trades,
        "표본 외 종이매매 평균 > 0": _number(paper_mean) is not None and paper_mean > 0,
        "리포트가 바이낸스 COIN-M 데이터": backtest.get("symbol") == settings.symbol and
            bool(backtest.get("contract")),
        "설정 지문 일치": backtest.get("fingerprint") == fp_now,
    }
    return {
        "source": SOURCE,
        "report_version": REPORT_VERSION,
        "validation_policy": settings.validation_policy(),
        "generated_at": now,
        "fingerprint": fp_now,
        "strategy_version": STRATEGY_VERSION,
        "symbol": settings.symbol,
        "chosen": backtest.get("chosen"),
        "passed": all(checks.values()),
        "checks": checks,
        "metrics": {"backtest_trades": s.get("n"), "avg_return": s.get("avg_return"),
                    "expectancy_btc": s.get("expectancy_btc"), "max_drawdown": dd, "dsr": dsr,
                    "pbo": pbo, "wf_oos_avg": wf.get("default_oos_avg_return"),
                    "wf_positive_folds": wf.get("default_positive_folds"), "wf_folds": folds,
                    "paper_trades": paper_n, "paper_mean_return": paper_mean},
        "backtest_generated_at": backtest.get("generated_at"),
        "backtest_data_end": (backtest.get("data") or {}).get("end"),
    }


def _number(x: Any) -> Optional[float]:
    try:
        value = float(x)
        return value if math.isfinite(value) and not isinstance(x, bool) else None
    except (TypeError, ValueError, OverflowError):
        return None


def save_report(settings: Settings, db: Any, report: Dict[str, Any]) -> str:
    db.insert_validation_report(report)
    p = Path(settings.state_dir) / "validation_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    return str(p)


class ValidationGate:
    def __init__(self, settings: Settings, db: Any, contract_essentials: Dict[str, Any],
                 clock=time.time):
        self.settings = settings
        self.db = db
        self.contract_essentials = contract_essentials
        self.clock = clock

    def check(self) -> Tuple[bool, str]:
        rep = self.db.latest_validation_report()
        if not rep:
            return False, "바이낸스 검증 리포트 없음 (python -m binance_coinm_v1 validate)"
        if rep.get("source") != SOURCE:
            return False, "리포트 출처가 바이낸스 COIN-M V1 이 아님"
        if rep.get("report_version") != REPORT_VERSION:
            return False, "검증 리포트 형식이 오래됨 - 재검증 필요"
        generated = _number(rep.get("generated_at"))
        now = self.clock()
        if generated is None or generated <= 0 or generated > now:
            return False, "검증 리포트 생성 시각 오류"
        age_d = (now - generated) / 86400
        if age_d > self.settings.validation_max_age_days:
            return False, f"검증 리포트가 오래됨 ({age_d:.1f}일 > {self.settings.validation_max_age_days:g}일)"
        fp = self.settings.fingerprint(self.contract_essentials)
        if rep.get("fingerprint") != fp:
            return False, "설정/계약 지문이 리포트와 다름 - 재검증 필요"
        if rep.get("validation_policy") != self.settings.validation_policy():
            return False, "검증 기준이 변경됨 - 재검증 필요"
        checks = rep.get("checks")
        if rep.get("passed") is not True or not isinstance(checks, dict) or not checks or \
                any(v is not True for v in checks.values()):
            failed = [k for k, v in checks.items() if v is not True] if isinstance(checks, dict) else ["검증 항목 형식 오류"]
            return False, "검증 미통과: " + ", ".join(failed)
        return True, f"검증 통과 ({age_d:.1f}일 전)"
