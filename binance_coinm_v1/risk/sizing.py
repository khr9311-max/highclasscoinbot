"""
BTC 기준 포지션 사이징 (COIN-M 인버스). 레버리지는 수량 계산에 쓰지 않는다.

순서 (요청 명세 그대로):
   1. equity (BTC)
   2. 위험 예산 (BTC) = equity x 1회 위험 비율
   3. 진입가 (예상 체결가 = 현재가에 진입 슬리피지 반영)
   4. 손절가 (예상 체결가 = 손절가에 손절 슬리피지 반영)
   5. 손절 거리
   6. 원하는 노출: 1계약 손실(BTC) = CS x |1/손절 - 1/진입| + 진입·청산 수수료
                   원하는 계약 수 = 예산 / 1계약 손실
   7. contractSize (exchangeInfo 값)
   8. 계약 수 = 내림 (절대 올리지 않는다 - 올리면 예산 초과)
   9. stepSize / minQty / maxQty 검증 (시장가 주문 한도 포함), 노출 상한
  10. 필요 증거금 = 명목(BTC) / 레버리지 (가용 잔고보다 크면 줄이거나 거부 - 레버리지를
      올려서 맞추지 않는다)
  11. 예상 수수료 (진입 + 손절 청산)
  12. 예상 펀딩 비용 (현재 펀딩비율 x 최대 보유 기간의 펀딩 횟수, 참고용 - V1 은
      펀딩을 진입 필터로 쓰지 않는다)
  13. 청산가 거리: 청산가까지 거리가 손절 거리의 liq_guard_min_ratio 배 이상이어야 한다

실제 체결가는 이 추정이 아니라 체결 조회 결과를 쓴다 (execution 계층).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional

from ..exchange.contract import ContractSpec
from . import inverse_math as im


@dataclass
class SizingInput:
    equity_btc: float
    available_btc: float
    risk_fraction: float
    direction: int
    entry_price: float
    stop_price: float
    leverage: int
    taker_fee: float
    entry_slippage_bps: float = 0.0
    stop_slippage_bps: float = 0.0
    max_exposure_multiple: float = 3.0
    funding_rate: float = 0.0
    expected_funding_periods: float = 0.0
    mmr: Optional[float] = None
    cum_btc: float = 0.0
    liq_guard_min_ratio: float = 2.0


@dataclass
class SizingResult:
    ok: bool
    reason: str
    qty: Decimal
    steps: Dict[str, Any] = field(default_factory=dict)

    @property
    def planned_loss_btc(self) -> float:
        return float(self.steps.get("loss_at_stop_btc", 0.0))


def _reject(reason: str, steps: Dict[str, Any]) -> SizingResult:
    return SizingResult(False, reason, Decimal(0), steps)


def size_position(inp: SizingInput, contract: ContractSpec) -> SizingResult:
    s: Dict[str, Any] = {}
    d = inp.direction
    if d not in (1, -1):
        return _reject("방향 오류", s)

    # 1. equity
    s["1_equity_btc"] = inp.equity_btc
    if not inp.equity_btc > 0:
        return _reject("equity 0 이하", s)
    # 2. 위험 예산
    budget = inp.equity_btc * inp.risk_fraction
    s["2_risk_budget_btc"] = budget
    # 3. 진입가
    side_in = "BUY" if d > 0 else "SELL"
    entry = im.price_with_slippage(inp.entry_price, side_in, inp.entry_slippage_bps)
    s["3_entry_price"] = inp.entry_price
    s["3_entry_price_eff"] = entry
    # 4. 손절가
    side_out = "SELL" if d > 0 else "BUY"
    stop = im.price_with_slippage(inp.stop_price, side_out, inp.stop_slippage_bps)
    s["4_stop_price"] = inp.stop_price
    s["4_stop_price_eff"] = stop
    if d * (inp.entry_price - inp.stop_price) <= 0:
        return _reject("손절이 진입가의 반대편이 아님", s)
    # 5. 손절 거리
    dist = abs(entry - stop) / entry
    s["5_stop_distance_pct"] = dist * 100
    # 7. contractSize (6단계 계산에 필요하므로 먼저 읽는다 - 기록 순서는 명세대로)
    cs = float(contract.contract_size)
    # 6. 원하는 노출
    loss_pc = abs(im.pnl_per_contract_btc(d, cs, entry, stop))
    fee_in_pc = im.fee_btc(1, cs, entry, inp.taker_fee)
    fee_out_pc = im.fee_btc(1, cs, stop, inp.taker_fee)
    per_contract = loss_pc + fee_in_pc + fee_out_pc
    desired = budget / per_contract
    s["6_loss_per_contract_btc"] = per_contract
    s["6_desired_contracts"] = desired
    s["6_desired_exposure_btc"] = desired * cs / entry
    s["6_desired_exposure_usd"] = desired * cs
    s["7_contract_size"] = str(contract.contract_size)
    # 8. 계약 수 (내림)
    qty = contract.round_qty_down(Decimal(repr(desired)), market=True)
    s["8_contracts_raw"] = str(qty)
    # 9. step/min/max + 노출 상한
    if qty < contract.market_min_qty:
        s["9_min_qty"] = str(contract.market_min_qty)
        return _reject(f"위험 예산으로 최소 수량({contract.market_min_qty}계약)도 못 삼 "
                       f"(1계약 손실 {per_contract:.8f} > 예산 {budget:.8f} BTC)", s)
    if qty > contract.market_max_qty:
        qty = contract.round_qty_down(contract.market_max_qty)
        s["9_capped_by"] = "market_max_qty"
    cap_btc = inp.equity_btc * inp.max_exposure_multiple
    if im.notional_btc(qty, cs, entry) > cap_btc:
        qty = contract.round_qty_down(Decimal(repr(cap_btc * entry / cs)))
        s["9_capped_by"] = "max_exposure_multiple"
    ok, why = contract.check_qty(qty, market=True)
    if not ok:
        return _reject(f"수량 검증 실패: {why}", s)
    s["9_contracts"] = str(qty)
    # 10. 필요 증거금
    margin = im.initial_margin_btc(qty, cs, entry, inp.leverage)
    fee_in = im.fee_btc(qty, cs, entry, inp.taker_fee)
    if margin + fee_in > inp.available_btc:
        afford = inp.available_btc / (cs / entry / inp.leverage + cs / entry * inp.taker_fee)
        qty = contract.round_qty_down(Decimal(repr(max(0.0, afford))))
        s["10_reduced_for_margin"] = True
        if qty < contract.market_min_qty:
            s["10_required_margin_btc"] = margin
            return _reject(f"가용 증거금 부족 (필요 {margin + fee_in:.8f} > 가용 "
                           f"{inp.available_btc:.8f} BTC) - 레버리지를 올려 맞추지 않음", s)
        margin = im.initial_margin_btc(qty, cs, entry, inp.leverage)
        fee_in = im.fee_btc(qty, cs, entry, inp.taker_fee)
    s["10_required_margin_btc"] = margin
    s["10_leverage"] = inp.leverage
    # 11. 수수료
    fee_out = im.fee_btc(qty, cs, stop, inp.taker_fee)
    s["11_est_entry_fee_btc"] = fee_in
    s["11_est_exit_fee_btc"] = fee_out
    # 12. 펀딩 (참고)
    notional = im.notional_btc(qty, cs, entry)
    s["12_funding_rate"] = inp.funding_rate
    s["12_est_funding_btc"] = -abs(notional * inp.funding_rate) * inp.expected_funding_periods
    # 13. 청산가 거리
    mmr = inp.mmr if inp.mmr is not None else contract.maint_margin_rate
    liq = im.liquidation_price(d, qty, cs, entry, margin, mmr, inp.cum_btc)
    liq_dist = abs(liq - entry) / entry if math.isfinite(liq) and liq > 0 else math.inf
    s["13_liquidation_price"] = liq
    s["13_liquidation_distance_pct"] = liq_dist * 100 if math.isfinite(liq_dist) else None
    loss = float(qty) * per_contract
    s["loss_at_stop_btc"] = loss
    s["notional_btc"] = notional
    s["notional_usd"] = im.notional_usd(qty, cs)
    s["effective_leverage"] = notional / inp.equity_btc
    if liq_dist < inp.liq_guard_min_ratio * dist:
        return _reject(f"청산가가 손절에 너무 가까움 (청산 {liq_dist * 100:.2f}% < "
                       f"손절 {dist * 100:.2f}% x {inp.liq_guard_min_ratio})", s)
    if loss > budget * (1 + 1e-9):
        return _reject("계산 오류: 손실이 예산 초과", s)
    return SizingResult(True, "ok", qty, s)


def liquidation_guard_ok(direction: int, entry: float, stop: float, liq_price: float,
                         min_ratio: float) -> bool:
    """체결 후 거래소가 알려준 실제 청산가로 다시 확인한다."""
    if not liq_price or liq_price <= 0 or not math.isfinite(liq_price):
        return True                                       # 청산 불가(숏 과증거금 등)
    if direction > 0 and liq_price >= stop:
        return False
    if direction < 0 and liq_price <= stop:
        return False
    return abs(liq_price - entry) >= min_ratio * abs(entry - stop)
