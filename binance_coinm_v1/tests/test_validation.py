"""12단계: 바이낸스 전용 검증 게이트 (기본 닫힘, 업비트 결과 무관)."""

import json
from pathlib import Path

import pytest

from binance_coinm_v1.config import Settings
from binance_coinm_v1.exchange.rest_client import LIVE_REST
from binance_coinm_v1.execution.live_gate import LiveOrderGate
from binance_coinm_v1.validation import (ValidationGate, build_report, paper_trade_returns,
                                         save_report)

from .helpers import live_settings, load_spec

SPEC = load_spec()
ESS = SPEC.essentials()
NOW = 1_790_000_000.0


def good_backtest(settings, **over):
    bt = {
        "symbol": "BTCUSD_PERP", "contract": ESS, "chosen": "ladder|both",
        "fingerprint": settings.fingerprint(ESS), "generated_at": NOW - 3600,
        "data": {"end": NOW - 86400 * 30},
        "chosen_summary": {"n": 150, "avg_return": 0.002, "expectancy_btc": 0.0002,
                           "max_drawdown": 0.12},
        "trial": {"dsr": 0.97, "pbo": 0.2},
        "walk_forward": {"default_oos_avg_return": 0.001, "default_positive_folds": 4,
                         "evaluated_folds": 5},
    }
    for k, v in over.items():
        path = k.split(".")
        d = bt
        for p in path[:-1]:
            d = d[p]
        d[path[-1]] = v
    return bt


def paper(n=40, mean=0.001):
    return [{"trade_id": str(i), "net_btc": mean, "ret": mean, "direction": 1, "closed_at": NOW,
             "fingerprint": Settings.build().fingerprint(ESS), "market_environment": "live"}
            for i in range(n)]


def test_all_checks_pass_opens_gate(db):
    s = Settings.build(state_dir=str(Path(db.path).parent))
    rep = build_report(s, good_backtest(s), paper(), ESS, now=NOW)
    assert rep["passed"], rep["checks"]
    save_report(s, db, rep)
    ok, why = ValidationGate(s, db, ESS, clock=lambda: NOW + 3600).check()
    assert ok, why


@pytest.mark.parametrize("over,paper_kw,failed", [
    ({"chosen_summary.n": 80}, {}, "백테스트 거래"),
    ({"chosen_summary.avg_return": -0.001}, {}, "기대값"),
    ({"chosen_summary.max_drawdown": 0.45}, {}, "최대낙폭"),
    ({"trial.dsr": 0.5}, {}, "DSR"),
    ({"trial.dsr": float("nan")}, {}, "DSR"),
    ({"trial.pbo": 0.7}, {}, "PBO"),
    ({"walk_forward.default_oos_avg_return": -0.002}, {}, "워크포워드"),
    ({"walk_forward.default_positive_folds": 1}, {}, "워크포워드"),
    ({}, {"n": 10}, "종이매매 30건"),
    ({}, {"mean": -0.001}, "종이매매 평균"),
    ({"fingerprint": "deadbeef"}, {}, "지문"),
    ({"symbol": "KRW-BTC"}, {}, "바이낸스"),
])
def test_each_failed_check_keeps_gate_closed(db, over, paper_kw, failed):
    s = Settings.build(state_dir=str(Path(db.path).parent))
    rep = build_report(s, good_backtest(s, **over), paper(**paper_kw), ESS, now=NOW)
    assert not rep["passed"]
    assert any(failed in k for k, v in rep["checks"].items() if not v)
    save_report(s, db, rep)
    ok, why = ValidationGate(s, db, ESS, clock=lambda: NOW).check()
    assert not ok and "미통과" in why


def test_gate_closed_without_report_stale_or_changed_settings(db):
    s = Settings.build(state_dir=str(Path(db.path).parent))
    gate = ValidationGate(s, db, ESS, clock=lambda: NOW)
    assert gate.check()[0] is False                                    # 리포트 없음
    save_report(s, db, build_report(s, good_backtest(s), paper(), ESS, now=NOW))
    assert ValidationGate(s, db, ESS, clock=lambda: NOW + 4 * 86400).check()[0] is False  # 오래됨
    s2 = Settings.build(min_rr=1.5)                                    # 설정 변경
    ok, why = ValidationGate(s2, db, ESS, clock=lambda: NOW).check()
    assert not ok and "지문" in why
    ess2 = dict(ESS, contract_size="10")                               # 계약 사양 변경
    assert ValidationGate(s, db, ess2, clock=lambda: NOW).check()[0] is False


def test_upbit_passed_report_is_ignored(tmp_path, db):
    # 업비트 봇의 통과 리포트가 옆에 있어도 바이낸스 게이트는 열리지 않는다
    upbit = tmp_path / "naked_validation.json"
    upbit.write_text(json.dumps({"passed": True, "dsr": 0.99, "pbo": 0.1,
                                 "generated_at": NOW}), encoding="utf-8")
    s = Settings.build(state_dir=str(tmp_path))
    ok, why = ValidationGate(s, db, ESS, clock=lambda: NOW).check()
    assert not ok and "리포트 없음" in why


def test_live_gate_uses_validation_gate(db):
    s = live_settings(state_dir=str(Path(db.path).parent))
    vg = ValidationGate(s, db, ESS, clock=lambda: NOW)
    gate = LiveOrderGate(s, LIVE_REST, vg.check)
    assert not gate.is_open()                                          # 리포트 없음 -> 닫힘
    save_report(s, db, build_report(s, good_backtest(s), paper(), ESS, now=NOW))
    assert gate.is_open()


def test_paper_sample_is_out_of_sample_only(db):
    base = {"symbol": "BTCUSD_PERP", "mode": "paper", "direction": 1, "state": "CLOSED",
            "entry_avg_price": 80000.0, "equity_at_entry_btc": 0.01, "closed_at": NOW,
            "created_at": NOW, "accounting": {"net_pnl_btc": 0.0001, "accounting_complete": True},
            "validation_fingerprint": Settings.build().fingerprint(ESS), "market_environment": "live"}
    db.upsert_position(dict(base, trade_id="a" * 10, signal={"close_time": NOW - 100}))
    db.upsert_position(dict(base, trade_id="b" * 10, signal={"close_time": NOW - 10 ** 7}))
    db.upsert_position(dict(base, trade_id="c" * 10, signal={"close_time": NOW - 50}, adopted=True))
    out = paper_trade_returns(db, after_ts=NOW - 10 ** 6, symbol="BTCUSD_PERP",
                              fingerprint=Settings.build().fingerprint(ESS))
    assert [p["trade_id"] for p in out] == ["a" * 10]                 # 표본 내·고아 채택 제외
    assert out[0]["ret"] == pytest.approx(0.01)
