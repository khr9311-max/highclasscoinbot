"""8단계: COIN-M 수식 · BTC 사이징 · 레버리지 독립성 · 청산가 · 수수료 · 펀딩 · 한도."""

import math
from decimal import Decimal

import pytest

from binance_coinm_v1.config import Settings
from binance_coinm_v1.risk import inverse_math as im
from binance_coinm_v1.risk.limits import RiskLimits
from binance_coinm_v1.risk.sizing import SizingInput, liquidation_guard_ok, size_position

from .helpers import load_spec

BTC = load_spec("BTCUSD_PERP")
ETH = load_spec("ETHUSD_PERP")


def inp(**kw):
    base = dict(equity_btc=0.007, available_btc=0.007, risk_fraction=0.005, direction=1,
                entry_price=84000.0, stop_price=83160.0, leverage=3, taker_fee=0.0005,
                entry_slippage_bps=0.0, stop_slippage_bps=0.0, max_exposure_multiple=3.0,
                liq_guard_min_ratio=2.0)
    base.update(kw)
    return SizingInput(**base)


# ---------------------------------------------------------------- COIN-M 수식
def test_notional_and_pnl_are_inverse_btc():
    assert im.notional_btc(3, 100, 80000.0) == pytest.approx(0.00375)
    assert im.notional_usd(3, 100) == 300
    p = im.pnl_btc(1, 3, 100, 80000.0, 88000.0)
    assert p == pytest.approx(300 * (1 / 80000 - 1 / 88000))
    assert p * 88000.0 == pytest.approx(30.0)                  # USD 가치 = 300 x 10%
    assert im.pnl_btc(-1, 3, 100, 80000.0, 88000.0) == pytest.approx(-p)
    # 인버스 비대칭: 같은 % 라도 BTC 손익 크기가 다르다
    up = im.pnl_btc(1, 1, 100, 80000.0, 88000.0)
    dn = im.pnl_btc(1, 1, 100, 80000.0, 72000.0)
    assert abs(dn) > abs(up)


def test_fees_and_funding_in_btc():
    assert im.fee_btc(4, 100, 80000.0, 0.0005) == pytest.approx(400 / 80000 * 0.0005)
    assert im.funding_fee_btc(1, 4, 100, 80000.0, 0.0001) == pytest.approx(-0.005 * 0.0001)
    assert im.funding_fee_btc(-1, 4, 100, 80000.0, 0.0001) == pytest.approx(0.005 * 0.0001)
    assert im.funding_fee_btc(1, 4, 100, 80000.0, -0.0001) > 0     # 음수 비율이면 롱이 받음


def test_liquidation_price_isolated():
    q, cs, e, lev, mmr = 4, 100, 80000.0, 3, 0.025
    w = im.initial_margin_btc(q, cs, e, lev)
    lp_long = im.liquidation_price(1, q, cs, e, w, mmr)
    lp_short = im.liquidation_price(-1, q, cs, e, w, mmr)
    assert lp_long == pytest.approx(e * (1 + mmr) / (1 / lev + 1))       # 약 -23%
    assert lp_short == pytest.approx(e * (1 - mmr) / (1 - 1 / lev))      # 약 +46%
    assert 0.76 < lp_long / e < 0.78 and 1.45 < lp_short / e < 1.47
    # 레버리지를 올리면 청산가가 가까워진다
    w10 = im.initial_margin_btc(q, cs, e, 10)
    assert im.liquidation_price(1, q, cs, e, w10, mmr) > lp_long
    assert im.liquidation_price(-1, q, cs, e, 1.0, mmr) == math.inf     # 과증거금 숏


def test_avg_entry_is_harmonic():
    assert im.avg_entry_price(1, 80000.0, 1, 90000.0) == pytest.approx(2 / (1 / 80000 + 1 / 90000))


# ---------------------------------------------------------------- 사이징
def test_user_example_budget_and_contracts():
    r = size_position(inp(), BTC)
    s = r.steps
    assert r.ok, r.reason
    assert s["2_risk_budget_btc"] == pytest.approx(0.000035)            # 0.007 x 0.5%
    assert s["5_stop_distance_pct"] == pytest.approx(1.0)
    per = 100 * (1 / 83160 - 1 / 84000) + 100 / 84000 * 0.0005 + 100 / 83160 * 0.0005
    assert s["6_loss_per_contract_btc"] == pytest.approx(per)
    assert r.qty == Decimal(2)                                          # floor(2.647)
    assert r.planned_loss_btc <= s["2_risk_budget_btc"]
    assert s["10_required_margin_btc"] == pytest.approx(2 * 100 / 84000 / 3)
    assert list(k for k in s if k[0].isdigit())[:3] == ["1_equity_btc", "2_risk_budget_btc",
                                                         "3_entry_price"]


def test_short_sizing_symmetric_rules():
    r = size_position(inp(direction=-1, entry_price=84000.0, stop_price=84840.0), BTC)
    assert r.ok and r.qty >= 1 and r.planned_loss_btc <= r.steps["2_risk_budget_btc"]
    bad = size_position(inp(direction=-1, entry_price=84000.0, stop_price=83000.0), BTC)
    assert not bad.ok and "반대편" in bad.reason


def test_leverage_does_not_change_quantity_only_margin():
    r3 = size_position(inp(equity_btc=0.1, available_btc=0.1, leverage=3), BTC)
    r10 = size_position(inp(equity_btc=0.1, available_btc=0.1, leverage=10), BTC)
    assert r3.qty == r10.qty
    assert r3.steps["10_required_margin_btc"] == pytest.approx(
        r10.steps["10_required_margin_btc"] * 10 / 3)


def test_min_qty_never_rounded_up():
    r = size_position(inp(equity_btc=0.001, available_btc=0.001), BTC)   # 예산 0.000005 BTC
    assert not r.ok and r.qty == 0 and "최소 수량" in r.reason


def test_contract_size_changes_quantity():
    r_btc = size_position(inp(equity_btc=0.05, available_btc=0.05, entry_price=3000.0,
                              stop_price=2970.0), BTC)
    r_eth = size_position(inp(equity_btc=0.05, available_btc=0.05, entry_price=3000.0,
                              stop_price=2970.0), ETH)
    assert r_eth.qty == pytest.approx(r_btc.qty * 10, abs=10)           # CS 100 vs 10


def test_margin_insufficient_reduces_then_rejects():
    r = size_position(inp(equity_btc=0.05, available_btc=0.0015), BTC)
    assert r.ok and r.steps.get("10_reduced_for_margin")
    assert r.steps["10_required_margin_btc"] + r.steps["11_est_entry_fee_btc"] <= 0.0015
    r2 = size_position(inp(equity_btc=0.05, available_btc=0.0001), BTC)
    assert not r2.ok and "증거금 부족" in r2.reason


def test_exposure_cap():
    r = size_position(inp(equity_btc=1.0, available_btc=1.0, stop_price=83900.0,
                          max_exposure_multiple=1.0), BTC)
    assert r.ok and r.steps.get("9_capped_by") == "max_exposure_multiple"
    assert r.steps["notional_btc"] <= 1.0 + 1e-9


def test_liquidation_guard_rejects_wide_stop_with_high_leverage():
    r = size_position(inp(equity_btc=1.0, available_btc=1.0, leverage=20,
                          stop_price=84000.0 * 0.97), BTC)
    assert not r.ok and "청산가" in r.reason
    ok = size_position(inp(equity_btc=1.0, available_btc=1.0, leverage=3,
                           stop_price=84000.0 * 0.97), BTC)
    assert ok.ok and ok.steps["13_liquidation_distance_pct"] > 20
    assert liquidation_guard_ok(1, 84000, 83000, 64000, 2.0)
    assert not liquidation_guard_ok(1, 84000, 83000, 83500, 2.0)        # 청산가가 손절보다 안쪽
    assert not liquidation_guard_ok(-1, 84000, 85000, 84500, 2.0)


def test_slippage_and_fees_reduce_size():
    clean = size_position(inp(equity_btc=0.5, available_btc=0.5), BTC)
    costly = size_position(inp(equity_btc=0.5, available_btc=0.5, entry_slippage_bps=20,
                               stop_slippage_bps=50), BTC)
    assert costly.qty < clean.qty


def test_funding_estimate_reported_not_filtered():
    r = size_position(inp(funding_rate=0.001, expected_funding_periods=9), BTC)
    assert r.ok and r.steps["12_est_funding_btc"] < 0


# ---------------------------------------------------------------- 한도
def test_daily_loss_limit_blocks_until_next_utc_day(db):
    s = Settings.build(max_daily_loss_pct=2.0)
    lim = RiskLimits(s, db, "paper")
    day1 = 1_790_000_000.0
    assert lim.check_new_entry(0.0100, 0, day1)[0]
    assert lim.check_new_entry(0.0099, 0, day1 + 60)[0]                # -1%
    ok, why = lim.check_new_entry(0.0097, 0, day1 + 120)                # -3%
    assert not ok and "일일 손실" in why
    assert db.risk_events("paper", "daily_loss_limit")
    ok2, _ = lim.check_new_entry(0.0097, 0, day1 + 86400)               # 다음 날: 새 기준
    assert ok2


def test_max_positions(db):
    lim = RiskLimits(Settings.build(), db, "paper")
    ok, why = lim.check_new_entry(0.01, 1, 1_790_000_000.0)
    assert not ok and "포지션" in why
