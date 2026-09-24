"""
COIN-M 인버스 계약 수식. 전부 BTC 단위 (USD 로 환산할 때만 가격을 곱한다).

  명목가치(BTC)   N = Q x CS / P                  (Q=계약 수, CS=contractSize(USD), P=가격)
  손익(BTC)       PnL = d x Q x CS x (1/진입 - 1/청산)   (d=+1 롱, -1 숏)
  초기증거금(BTC) IM = N(진입가) / 레버리지
  수수료(BTC)     fee = N(체결가) x 수수료율
  펀딩(BTC)       F = -d x N(마크가) x 펀딩비율  (양수 비율이면 롱이 낸다)
  청산가 (격리, 단일 포지션, Binance COIN-M 식)
      롱: LP = Q·CS·(1+mmr) / (W + cum + Q·CS/진입)
      숏: LP = Q·CS·(1-mmr) / (Q·CS/진입 - W - cum)
      W = 격리 지갑(BTC), mmr = 유지증거금률, cum = 구간 유지증거금 공제액(BTC)
  평균 진입가     인버스는 '1/가격' 을 계약 수로 가중평균한 조화평균

USDⓈ-M 식(수량 x 가격)을 쓰지 않는다. contractSize 는 호출부가 exchangeInfo 값으로 준다.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _q(qty: Any) -> float:
    return float(qty)


def notional_btc(qty: Any, contract_size: Any, price: float) -> float:
    if price <= 0:
        raise ValueError("가격은 0보다 커야 함")
    return _q(qty) * float(contract_size) / price


def notional_usd(qty: Any, contract_size: Any) -> float:
    return _q(qty) * float(contract_size)


def pnl_btc(direction: int, qty: Any, contract_size: Any, entry: float, exit_price: float) -> float:
    if entry <= 0 or exit_price <= 0:
        raise ValueError("가격은 0보다 커야 함")
    return direction * _q(qty) * float(contract_size) * (1.0 / entry - 1.0 / exit_price)


def pnl_per_contract_btc(direction: int, contract_size: Any, entry: float, exit_price: float) -> float:
    return pnl_btc(direction, 1, contract_size, entry, exit_price)


def initial_margin_btc(qty: Any, contract_size: Any, price: float, leverage: float) -> float:
    if leverage <= 0:
        raise ValueError("레버리지는 0보다 커야 함")
    return notional_btc(qty, contract_size, price) / leverage


def fee_btc(qty: Any, contract_size: Any, price: float, rate: float) -> float:
    return notional_btc(qty, contract_size, price) * rate


def funding_fee_btc(direction: int, qty: Any, contract_size: Any, mark_price: float,
                    funding_rate: float) -> float:
    """받으면 +, 내면 -."""
    return -direction * notional_btc(qty, contract_size, mark_price) * funding_rate


def liquidation_price(direction: int, qty: Any, contract_size: Any, entry: float,
                      isolated_wallet_btc: float, mmr: float, cum_btc: float = 0.0) -> float:
    """격리 증거금 단일(one-way) 포지션의 청산가. 숏이 청산 불가하면 inf."""
    q = _q(qty) * float(contract_size)
    if q <= 0:
        return 0.0 if direction > 0 else math.inf
    if direction > 0:
        return q * (1.0 + mmr) / (isolated_wallet_btc + cum_btc + q / entry)
    denom = q / entry - isolated_wallet_btc - cum_btc
    if denom <= 0:
        return math.inf
    return q * (1.0 - mmr) / denom


def avg_entry_price(old_qty: Any, old_entry: float, add_qty: Any, add_price: float) -> float:
    oq, aq = _q(old_qty), _q(add_qty)
    if oq <= 0:
        return add_price
    return (oq + aq) / (oq / old_entry + aq / add_price)


def price_with_slippage(price: float, side: str, bps: float) -> float:
    """시장가 체결 가정: 사면 비싸게, 팔면 싸게."""
    s = bps / 10_000.0
    return price * (1.0 + s) if side == "BUY" else price * (1.0 - s)


def select_bracket(brackets: Sequence[Dict[str, Any]], notional_base: float) -> Tuple[float, float]:
    """
    leverageBracket 에서 (mmr, cum) 선택. COIN-M 구간 상한(qtyCap)은 기초자산(BTC) 수량.
    구간 정보가 없으면 호출부가 exchangeInfo 의 maintMarginPercent 로 대체한다.
    """
    for b in sorted(brackets, key=lambda x: float(x.get("qtyCap", x.get("notionalCap", 0)) or 0)):
        cap = float(b.get("qtyCap", b.get("notionalCap", 0)) or 0)
        if notional_base <= cap:
            return float(b["maintMarginRatio"]), float(b.get("cum", 0.0) or 0.0)
    if brackets:
        last = max(brackets, key=lambda x: float(x.get("qtyCap", x.get("notionalCap", 0)) or 0))
        return float(last["maintMarginRatio"]), float(last.get("cum", 0.0) or 0.0)
    raise ValueError("leverage bracket 없음")
