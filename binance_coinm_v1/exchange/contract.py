"""
COIN-M 계약 사양 (exchangeInfo 에서 동적으로 읽는다 - 하드코딩 금지).

COIN-M 인버스 계약은 USDⓈ-M 과 수량 단위가 다르다:
  - 수량 = '계약 수'. 1계약의 명목가치 = contractSize (BTCUSD_PERP 는 USD 100 이지만
    이 값을 코드에 박지 않는다. ETHUSD_PERP 는 10 이다)
  - 명목가치(BTC) = 계약수 x contractSize / 가격
  - 증거금·손익·수수료가 전부 BTC (marginAsset)

시작할 때 이 사양을 확인하지 못하면(심볼 없음·거래 중지·필터 누락·증거금 자산 불일치)
ContractResolutionError 를 내고 거래를 중지한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ContractResolutionError

REQUIRED_ORDER_TYPES = ("MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET")


def D(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(str(v))


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    pair: str
    contract_type: str
    contract_status: str
    contract_size: Decimal
    margin_asset: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    percent_up: Optional[Decimal]
    percent_down: Optional[Decimal]
    max_num_orders: Optional[int]
    max_num_algo_orders: Optional[int]
    order_types: Tuple[str, ...]
    time_in_force: Tuple[str, ...]
    maint_margin_pct: Decimal
    required_margin_pct: Decimal
    trigger_protect: Optional[Decimal]
    liquidation_fee: Optional[Decimal]
    market_take_bound: Optional[Decimal]
    price_precision: int
    quantity_precision: int
    delivery_date_ms: int
    onboard_date_ms: int
    underlying_type: str

    # ------------------------------------------------------------------
    @property
    def contract_size_f(self) -> float:
        return float(self.contract_size)

    @property
    def maint_margin_rate(self) -> float:
        """exchangeInfo 의 첫 구간 유지증거금률 (leverageBracket 을 못 읽을 때의 대체값)."""
        return float(self.maint_margin_pct) / 100.0

    def round_price(self, price: Any, mode: str = "nearest") -> Decimal:
        p = D(price)
        rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
        n = (p / self.tick_size).to_integral_value(rounding=rounding)
        return (n * self.tick_size).quantize(self.tick_size)

    def round_price_away(self, price: Any, direction: int, is_stop: bool) -> Decimal:
        """
        전략 가격을 틱에 맞춘다. 규칙을 '느슨하게' 만들지 않는 방향으로:
          - 손절: 진입에서 더 먼 쪽 (롱 손절은 내림, 숏 손절은 올림)
          - 진입 트리거: 돌파를 더 확실히 요구하는 쪽 (롱 올림, 숏 내림)
        사이징은 반올림된 값으로 다시 계산하므로 위험 예산은 지켜진다.
        """
        if is_stop:
            return self.round_price(price, "down" if direction > 0 else "up")
        return self.round_price(price, "up" if direction > 0 else "down")

    def round_target(self, price: Any, direction: int) -> Decimal:
        """목표가는 진입 쪽으로 (체결 확률을 높이는 보수적 방향)."""
        return self.round_price(price, "down" if direction > 0 else "up")

    def round_qty_down(self, qty: Any, market: bool = True) -> Decimal:
        step = self.market_step_size if market else self.step_size
        q = D(qty)
        if q <= 0:
            return Decimal(0)
        n = (q / step).to_integral_value(rounding=ROUND_DOWN)
        return (n * step).quantize(step)

    def check_qty(self, qty: Any, market: bool = True) -> Tuple[bool, str]:
        q = D(qty)
        step = self.market_step_size if market else self.step_size
        lo = self.market_min_qty if market else self.min_qty
        hi = self.market_max_qty if market else self.max_qty
        if q <= 0:
            return False, "수량 0"
        if q < lo:
            return False, f"최소 수량 미만 ({q} < {lo})"
        if q > hi:
            return False, f"최대 수량 초과 ({q} > {hi})"
        if (q / step) != (q / step).to_integral_value():
            return False, f"stepSize({step}) 배수 아님"
        return True, ""

    def check_price(self, price: Any) -> Tuple[bool, str]:
        p = D(price)
        if p < self.min_price or p > self.max_price:
            return False, f"가격 범위 밖 ({p})"
        if (p / self.tick_size) != (p / self.tick_size).to_integral_value():
            return False, f"tickSize({self.tick_size}) 배수 아님"
        return True, ""

    def fmt_price(self, price: Any) -> str:
        return format(self.round_price(price), "f")

    def fmt_qty(self, qty: Any) -> str:
        return format(D(qty).quantize(self.step_size), "f")

    def supports(self, order_type: str) -> bool:
        return order_type in self.order_types

    def essentials(self) -> Dict[str, str]:
        """검증 지문에 넣는 값 - 이게 바뀌면 백테스트 가정이 달라진다."""
        return {"symbol": self.symbol, "contract_size": str(self.contract_size),
                "tick_size": str(self.tick_size), "step_size": str(self.step_size),
                "min_qty": str(self.min_qty), "margin_asset": self.margin_asset}

    def to_dict(self) -> Dict[str, Any]:
        out = {}
        for k in self.__dataclass_fields__:
            v = getattr(self, k)
            out[k] = str(v) if isinstance(v, Decimal) else (list(v) if isinstance(v, tuple) else v)
        return out


# ---------------------------------------------------------------------------
def _filters(raw: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f.get("filterType"): f for f in raw.get("filters") or [] if isinstance(f, dict)}


def _req(raw: Mapping[str, Any], key: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise ContractResolutionError(f"exchangeInfo 필드 누락: {raw.get('symbol')}.{key}")
    return raw[key]


def _dec_pos(v: Any, name: str) -> Decimal:
    try:
        d = D(v)
    except Exception:
        raise ContractResolutionError(f"{name} 숫자 아님: {v!r}") from None
    if d <= 0:
        raise ContractResolutionError(f"{name} 가 0 이하: {v!r}")
    return d


def parse_contract(raw: Mapping[str, Any]) -> ContractSpec:
    f = _filters(raw)
    for need in ("PRICE_FILTER", "LOT_SIZE"):
        if need not in f:
            raise ContractResolutionError(f"{raw.get('symbol')}: {need} 필터 없음")
    pf, lot = f["PRICE_FILTER"], f["LOT_SIZE"]
    mlot = f.get("MARKET_LOT_SIZE") or lot
    pp = f.get("PERCENT_PRICE") or {}

    def opt_dec(v):
        try:
            return D(v) if v not in (None, "") else None
        except Exception:
            return None

    spec = ContractSpec(
        symbol=str(_req(raw, "symbol")),
        pair=str(_req(raw, "pair")),
        contract_type=str(_req(raw, "contractType")),
        contract_status=str(_req(raw, "contractStatus")),
        contract_size=_dec_pos(_req(raw, "contractSize"), "contractSize"),
        margin_asset=str(_req(raw, "marginAsset")),
        base_asset=str(_req(raw, "baseAsset")),
        quote_asset=str(_req(raw, "quoteAsset")),
        tick_size=_dec_pos(pf.get("tickSize"), "tickSize"),
        min_price=D(pf.get("minPrice", "0")),
        max_price=_dec_pos(pf.get("maxPrice"), "maxPrice"),
        step_size=_dec_pos(lot.get("stepSize"), "stepSize"),
        min_qty=_dec_pos(lot.get("minQty"), "minQty"),
        max_qty=_dec_pos(lot.get("maxQty"), "maxQty"),
        market_step_size=_dec_pos(mlot.get("stepSize"), "MARKET_LOT_SIZE.stepSize"),
        market_min_qty=_dec_pos(mlot.get("minQty"), "MARKET_LOT_SIZE.minQty"),
        market_max_qty=_dec_pos(mlot.get("maxQty"), "MARKET_LOT_SIZE.maxQty"),
        percent_up=opt_dec(pp.get("multiplierUp")),
        percent_down=opt_dec(pp.get("multiplierDown")),
        max_num_orders=int(f["MAX_NUM_ORDERS"]["limit"]) if "MAX_NUM_ORDERS" in f else None,
        max_num_algo_orders=(int(f["MAX_NUM_ALGO_ORDERS"]["limit"])
                             if "MAX_NUM_ALGO_ORDERS" in f else None),
        order_types=tuple(raw.get("orderTypes") or ()),
        time_in_force=tuple(raw.get("timeInForce") or ()),
        maint_margin_pct=_dec_pos(_req(raw, "maintMarginPercent"), "maintMarginPercent"),
        required_margin_pct=_dec_pos(_req(raw, "requiredMarginPercent"), "requiredMarginPercent"),
        trigger_protect=opt_dec(raw.get("triggerProtect")),
        liquidation_fee=opt_dec(raw.get("liquidationFee")),
        market_take_bound=opt_dec(raw.get("marketTakeBound")),
        price_precision=int(raw.get("pricePrecision", 8)),
        quantity_precision=int(raw.get("quantityPrecision", 0)),
        delivery_date_ms=int(raw.get("deliveryDate", 0) or 0),
        onboard_date_ms=int(raw.get("onboardDate", 0) or 0),
        underlying_type=str(raw.get("underlyingType", "")),
    )
    if spec.min_qty > spec.max_qty or spec.market_min_qty > spec.market_max_qty:
        raise ContractResolutionError(f"{spec.symbol}: 최소 수량이 최대 수량보다 큼")
    return spec


def perpetual_candidates(exchange_info: Mapping[str, Any], margin_asset: str = "BTC") -> List[str]:
    return [s.get("symbol") for s in exchange_info.get("symbols") or []
            if s.get("contractType") == "PERPETUAL" and s.get("marginAsset") == margin_asset]


def resolve_contract(exchange_info: Optional[Mapping[str, Any]], symbol: str,
                     margin_asset: str = "BTC", base_asset: str = "BTC",
                     required_types: Iterable[str] = REQUIRED_ORDER_TYPES) -> ContractSpec:
    """설정된 심볼이 '지금 거래 가능한 BTC 증거금 무기한 계약' 인지 확인한다."""
    if not exchange_info or not isinstance(exchange_info, Mapping) or "symbols" not in exchange_info:
        raise ContractResolutionError("exchangeInfo 를 가져오지 못함 - 거래 중지")
    raw = next((s for s in exchange_info["symbols"] if s.get("symbol") == symbol), None)
    if raw is None:
        cands = perpetual_candidates(exchange_info, margin_asset)
        raise ContractResolutionError(f"심볼 {symbol} 없음 (BTC 증거금 무기한 후보: {cands})")
    spec = parse_contract(raw)
    if spec.contract_type != "PERPETUAL":
        raise ContractResolutionError(f"{symbol} 은 무기한 계약이 아님 ({spec.contract_type})")
    if spec.contract_status != "TRADING":
        raise ContractResolutionError(f"{symbol} 거래 불가 상태 ({spec.contract_status})")
    if spec.margin_asset != margin_asset:
        raise ContractResolutionError(f"{symbol} 증거금 자산이 {margin_asset} 아님 ({spec.margin_asset})")
    if spec.base_asset != base_asset:
        raise ContractResolutionError(f"{symbol} 기초자산이 {base_asset} 아님 ({spec.base_asset})")
    missing = [t for t in required_types if t not in spec.order_types]
    if missing:
        raise ContractResolutionError(f"{symbol} 주문 유형 미지원: {missing}")
    return spec
