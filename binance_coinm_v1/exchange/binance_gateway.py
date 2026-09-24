"""
실거래/테스트넷 게이트웨이 (Binance COIN-M dapi).

2026-06-30 CM-UM 통합 이후 반영 사항:
  - 조건부 주문(STOP_MARKET/TAKE_PROFIT_MARKET/...)은 POST /dapi/v1/algoOrder
    (algoType=CONDITIONAL, triggerPrice, clientAlgoId). /dapi/v1/order 로 보내면 -4120.
    조회는 GET /dapi/v1/openAlgoOrders · /dapi/v1/algoOrder, 취소는 DELETE /dapi/v1/algoOrder.
  - 주문 응답에 avgPrice 가 없다 -> 실제 체결가는 GET /dapi/v1/order 또는 userTrades 로 확인.
  - 포지션 모드(dualSidePosition)는 UM 과 공유된다 -> 이 봇은 절대 바꾸지 않는다
    (바꾸면 사용자의 USDⓈ-M 계정에도 영향). 원웨이가 아니면 거래를 멈춘다.

모든 변경 요청은 REST 클라이언트의 mutation_guard(LiveOrderGate)를 거친다.
결과를 모르는 오류는 OrderStatusUnknown 으로 올린다 (호출부가 조회로 확정).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .errors import (BinanceAPIError, ExchangeError, CODE_CANCEL_REJECTED, CODE_NO_NEED_MARGIN_TYPE,
                     CODE_NO_SUCH_ORDER, LiveOrderBlocked, OrderStatusUnknown, outcome_unknown)
from .gateway import ExchangeGateway
from .models import (ALGO_STATUS_MAP, AccountInfo, AssetBalance, Fill, OrderRequest, OrderState,
                     PositionInfo, dec, fnum)
from .rest_client import BinanceRestClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 파서 (바이낸스 원문 -> 공통 모델)
# ---------------------------------------------------------------------------
def parse_order(d: Dict[str, Any]) -> OrderState:
    avg = fnum(d.get("avgPrice"), 0.0)
    return OrderState(
        client_id=str(d.get("clientOrderId", "")), symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "")), order_type=str(d.get("type") or d.get("origType") or ""),
        status=str(d.get("status", "")), is_algo=False,
        exchange_id=str(d["orderId"]) if d.get("orderId") is not None else None,
        orig_qty=dec(d.get("origQty")), executed_qty=dec(d.get("executedQty")),
        avg_price=avg if avg > 0 else None,
        trigger_price=fnum(d.get("stopPrice"), 0.0) or None,
        reduce_only=_b(d.get("reduceOnly", False)),
        close_position=_b(d.get("closePosition", False)),
        working_type=d.get("workingType"),
        update_time_ms=int(d.get("updateTime") or d.get("time") or 0), raw=dict(d))


def parse_algo(d: Dict[str, Any]) -> OrderState:
    status = ALGO_STATUS_MAP.get(str(d.get("algoStatus", "")).upper(), str(d.get("algoStatus", "")))
    actual = d.get("actualOrderId")
    avg = fnum(d.get("actualPrice"), 0.0)
    return OrderState(
        client_id=str(d.get("clientAlgoId", "")), symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "")), order_type=str(d.get("orderType") or d.get("type") or ""),
        status=status, is_algo=True,
        exchange_id=str(d["algoId"]) if d.get("algoId") is not None else None,
        orig_qty=dec(d.get("quantity")), executed_qty=dec(d.get("actualQty")),
        avg_price=avg if avg > 0 else None,
        trigger_price=fnum(d.get("triggerPrice"), 0.0) or None,
        reduce_only=_b(d.get("reduceOnly")), close_position=_b(d.get("closePosition")),
        working_type=d.get("workingType"),
        actual_order_id=str(actual) if actual not in (None, "", 0, "0") else None,
        update_time_ms=int(d.get("updateTime") or d.get("createTime") or 0), raw=dict(d))


def parse_position(d: Dict[str, Any]) -> PositionInfo:
    return PositionInfo(
        symbol=str(d.get("symbol", "")), position_amt=dec(d.get("positionAmt")),
        entry_price=fnum(d.get("entryPrice")), mark_price=fnum(d.get("markPrice")),
        unrealized_pnl_btc=fnum(d.get("unRealizedProfit", d.get("unrealizedProfit"))),
        liquidation_price=fnum(d.get("liquidationPrice")),
        leverage=int(fnum(d.get("leverage"), 0)),
        margin_type=str(d.get("marginType", "isolated" if d.get("isolated") else "cross")).lower(),
        isolated_margin_btc=fnum(d.get("isolatedMargin", d.get("isolatedWallet"))),
        position_side=str(d.get("positionSide", "BOTH")),
        update_time_ms=int(d.get("updateTime") or 0),
        break_even_price=fnum(d.get("breakEvenPrice"), 0.0) or None)


def parse_asset(d: Dict[str, Any]) -> AssetBalance:
    return AssetBalance(
        asset=str(d.get("asset", "")), wallet_balance=fnum(d.get("walletBalance", d.get("balance"))),
        unrealized_pnl=fnum(d.get("unrealizedProfit", d.get("crossUnPnl"))),
        margin_balance=fnum(d.get("marginBalance", d.get("balance"))),
        available_balance=fnum(d.get("availableBalance")),
        initial_margin=fnum(d.get("initialMargin")),
        position_initial_margin=fnum(d.get("positionInitialMargin")),
        open_order_initial_margin=fnum(d.get("openOrderInitialMargin")),
        maint_margin=fnum(d.get("maintMargin")),
        max_withdraw=fnum(d.get("maxWithdrawAmount", d.get("withdrawAvailable"))),
        cross_wallet_balance=fnum(d.get("crossWalletBalance")))


def parse_trade(d: Dict[str, Any]) -> Fill:
    return Fill(symbol=str(d.get("symbol", "")), trade_id=str(d.get("id")),
                order_id=str(d.get("orderId")), side=str(d.get("side", "")),
                price=fnum(d.get("price")), qty=dec(d.get("qty")),
                realized_pnl_btc=fnum(d.get("realizedPnl")), commission=fnum(d.get("commission")),
                commission_asset=str(d.get("commissionAsset", "")),
                time_ms=int(d.get("time") or 0), maker=bool(d.get("maker", False)))


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() == "true"


# ---------------------------------------------------------------------------
class BinanceGateway(ExchangeGateway):
    def __init__(self, rest: BinanceRestClient, mode: str):
        super().__init__()
        if mode not in ("live", "testnet"):
            raise ValueError("BinanceGateway 는 live/testnet 전용 (paper 는 PaperGateway)")
        self.rest = rest
        self.mode = mode

    async def close(self) -> None:
        await self.rest.close()

    # ---- 계정 ----
    async def get_account(self) -> AccountInfo:
        d = await self.rest.signed("GET", "/dapi/v1/account")
        assets = {a["asset"]: parse_asset(a) for a in d.get("assets", [])}
        positions = [parse_position(p) for p in d.get("positions", [])]
        return AccountInfo(assets=assets, positions=positions, can_trade=bool(d.get("canTrade", True)),
                           fee_tier=int(d.get("feeTier", 0) or 0),
                           update_time_ms=int(d.get("updateTime", 0) or 0))

    async def get_balance(self, asset: str = "BTC") -> AssetBalance:
        rows = await self.rest.signed("GET", "/dapi/v1/balance")
        for r in rows or []:
            if r.get("asset") == asset:
                return parse_asset(r)
        return AssetBalance(asset, 0.0, 0.0, 0.0, 0.0)

    async def get_positions(self, symbol: str) -> List[PositionInfo]:
        pair = symbol.split("_")[0]
        rows = await self.rest.signed("GET", "/dapi/v1/positionRisk", {"pair": pair})
        return [parse_position(r) for r in rows or [] if r.get("symbol") == symbol]

    async def get_position_mode(self) -> bool:
        d = await self.rest.signed("GET", "/dapi/v1/positionSide/dual")
        return _b(d.get("dualSidePosition"))

    async def get_commission_rate(self, symbol: str) -> Tuple[float, float]:
        d = await self.rest.signed("GET", "/dapi/v1/commissionRate", {"symbol": symbol})
        return fnum(d.get("makerCommissionRate")), fnum(d.get("takerCommissionRate"))

    async def get_leverage_brackets(self, symbol: str) -> List[Dict[str, Any]]:
        d = await self.rest.signed("GET", "/dapi/v2/leverageBracket", {"symbol": symbol})
        rows = d if isinstance(d, list) else [d]
        for r in rows:
            if r.get("symbol") in (symbol, None):
                return list(r.get("brackets") or [])
        return []

    # ---- 주문 조회 ----
    async def get_open_orders(self, symbol: str) -> List[OrderState]:
        rows = await self.rest.signed("GET", "/dapi/v1/openOrders", {"symbol": symbol})
        return [parse_order(r) for r in rows or []]

    async def get_open_algo_orders(self, symbol: str) -> List[OrderState]:
        rows = await self.rest.signed("GET", "/dapi/v1/openAlgoOrders", {"symbol": symbol})
        if isinstance(rows, dict):                      # 일부 응답은 {"orders": [...]}
            rows = rows.get("orders") or rows.get("data") or []
        return [parse_algo(r) for r in rows or []]

    async def get_order(self, symbol: str, client_id: str) -> OrderState:
        try:
            d = await self.rest.signed("GET", "/dapi/v1/order",
                                       {"symbol": symbol, "origClientOrderId": client_id})
        except BinanceAPIError as e:
            if e.code == CODE_NO_SUCH_ORDER:
                return OrderState.not_found(client_id, symbol)
            raise
        return parse_order(d)

    async def get_algo_order(self, symbol: str, client_id: str) -> OrderState:
        try:
            d = await self.rest.signed("GET", "/dapi/v1/algoOrder", {"clientAlgoId": client_id})
        except BinanceAPIError as e:
            if e.code == CODE_NO_SUCH_ORDER or "not exist" in (e.msg or "").lower():
                return OrderState.not_found(client_id, symbol, is_algo=True)
            raise
        if not d:
            return OrderState.not_found(client_id, symbol, is_algo=True)
        return parse_algo(d)

    async def get_user_trades(self, symbol: str, start_ms: Optional[int] = None,
                              order_id: Optional[str] = None) -> List[Fill]:
        end = self.rest.now_ms()
        week = 7 * 86400_000
        start = int(start_ms) if start_ms is not None else end - week
        found = {}
        while start <= end:
            stop = min(start + week - 1, end)
            cursor = None
            while True:
                params: Dict[str, Any] = {"symbol": symbol, "limit": 1000}
                if order_id:
                    params["orderId"] = order_id
                if cursor is None:
                    params.update(startTime=start, endTime=stop)
                else:
                    # fromId cannot be combined with startTime/endTime. Time-only
                    # pagination would drop fills sharing the last millisecond.
                    params["fromId"] = cursor
                rows = await self.rest.signed("GET", "/dapi/v1/userTrades", params) or []
                for r in rows:
                    if start <= int(r["time"]) <= stop:
                        found[str(r["id"])] = parse_trade(r)
                if len(rows) < 1000 or any(int(r["time"]) > stop for r in rows):
                    break
                next_id = max(int(r["id"]) for r in rows) + 1
                if cursor is not None and next_id <= cursor:
                    raise ExchangeError("userTrades pagination did not advance")
                cursor = next_id
            start = stop + 1
        return sorted(found.values(), key=lambda f: (f.time_ms, int(f.trade_id)))

    async def get_income(self, symbol: str, income_type: Optional[str] = None,
                         start_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        end = self.rest.now_ms()
        start = int(start_ms) if start_ms is not None else end - 7 * 86400_000
        found = {}
        while start <= end:
            stop = min(start + 365 * 86400_000 - 1, end)
            page = 1
            while True:
                params = {"symbol": symbol, "incomeType": income_type, "startTime": start,
                          "endTime": stop, "limit": 1000, "page": page}
                rows = await self.rest.signed("GET", "/dapi/v1/income", params) or []
                before = len(found)
                for r in rows:
                    found[(r["incomeType"], str(r["tranId"]))] = r
                if len(rows) < 1000:
                    break
                if len(found) == before:
                    raise ExchangeError("income pagination did not advance")
                page += 1
            start = stop + 1
        return sorted(found.values(), key=lambda r: int(r["time"]))

    async def funding_marks(self, symbol: str, start_ms: int, end_ms: int) -> Dict[int, float]:
        from .market_data import MarketData
        rows = await MarketData(self.rest).funding_history(symbol, start_ms, end_ms)
        return {r.funding_time_ms: r.mark_price for r in rows if r.mark_price is not None}

    # ---- 주문 변경 ----
    async def place_order(self, req: OrderRequest) -> OrderState:
        req.validate()
        if req.is_conditional:
            path = "/dapi/v1/algoOrder"
            params: Dict[str, Any] = {
                "algoType": "CONDITIONAL", "symbol": req.symbol, "side": req.side,
                "positionSide": "BOTH", "type": req.order_type,
                "triggerPrice": req.trigger_price, "workingType": req.working_type,
                "priceProtect": bool(req.price_protect), "clientAlgoId": req.client_id}
            if req.close_position:
                params["closePosition"] = True
            else:
                params["quantity"] = req.quantity
                if req.reduce_only:
                    params["reduceOnly"] = True
        else:
            path = "/dapi/v1/order"
            params = {"symbol": req.symbol, "side": req.side, "positionSide": "BOTH",
                      "type": req.order_type, "quantity": req.quantity,
                      "newClientOrderId": req.client_id, "newOrderRespType": "RESULT"}
            if req.reduce_only:
                params["reduceOnly"] = True
            if req.order_type == "LIMIT":
                params["price"] = req.price
                params["timeInForce"] = req.time_in_force or "GTC"
        try:
            data = await self.rest.signed("POST", path, params)
        except LiveOrderBlocked:
            raise
        except Exception as e:
            if outcome_unknown(e):
                raise OrderStatusUnknown(req.client_id, e) from None
            raise
        return parse_algo(data) if req.is_conditional else parse_order(data)

    async def cancel_order(self, symbol: str, client_id: str, is_algo: bool) -> OrderState:
        try:
            if is_algo:
                await self.rest.signed("DELETE", "/dapi/v1/algoOrder", {"clientAlgoId": client_id})
            else:
                d = await self.rest.signed("DELETE", "/dapi/v1/order",
                                           {"symbol": symbol, "origClientOrderId": client_id})
                return parse_order(d)
        except LiveOrderBlocked:
            raise
        except BinanceAPIError as e:
            if e.code not in (CODE_CANCEL_REJECTED, CODE_NO_SUCH_ORDER):
                raise
            # 이미 체결·취소·발동된 주문. 실제 상태를 조회해서 돌려준다.
        except Exception as e:
            if outcome_unknown(e):
                raise OrderStatusUnknown(client_id, e) from None
            raise
        return await (self.get_algo_order(symbol, client_id) if is_algo
                      else self.get_order(symbol, client_id))

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self.rest.signed("POST", "/dapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            await self.rest.signed("POST", "/dapi/v1/marginType",
                                   {"symbol": symbol, "marginType": margin_type})
        except BinanceAPIError as e:
            if e.code != CODE_NO_NEED_MARGIN_TYPE:
                raise

    # ---- 사용자 데이터 스트림 (listenKey) ----
    async def create_listen_key(self) -> str:
        d = await self.rest.api_key_only("POST", "/dapi/v1/listenKey")
        return str(d["listenKey"])

    async def keepalive_listen_key(self) -> None:
        await self.rest.api_key_only("PUT", "/dapi/v1/listenKey")

    async def close_listen_key(self) -> None:
        await self.rest.api_key_only("DELETE", "/dapi/v1/listenKey")
