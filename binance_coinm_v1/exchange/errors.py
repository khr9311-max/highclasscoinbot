"""
거래소 오류 분류.

가장 중요한 구분은 '주문이 거래소에 도달했는지 모른다' 는 경우다. 이때 같은 주문을
다시 보내면 중복 주문이 된다. 이런 오류는 OrderStatusUnknown 계열로 올리고, 호출부는
반드시 clientOrderId 로 조회해 실제 상태를 확인한 뒤에만 다음 행동을 한다.
"""

from __future__ import annotations

from typing import Optional


class ExchangeError(Exception):
    """거래소 계층의 모든 오류."""


class NetworkError(ExchangeError):
    """연결 실패. maybe_sent=False 면 요청이 서버에 도달하지 않은 것이 확실하다."""

    def __init__(self, msg: str, maybe_sent: bool = True):
        super().__init__(msg)
        self.maybe_sent = maybe_sent


class RequestTimeout(NetworkError):
    """응답 대기 중 시간 초과 - 요청이 처리됐는지 알 수 없다."""

    def __init__(self, msg: str):
        super().__init__(msg, maybe_sent=True)


class RateLimited(ExchangeError):
    def __init__(self, msg: str, retry_after: float = 60.0, banned: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.banned = banned


class BinanceAPIError(ExchangeError):
    """거래소가 명시적으로 거부한 요청 (HTTP 4xx + code)."""

    def __init__(self, status: int, code: Optional[int], msg: str, path: str = ""):
        super().__init__(f"HTTP {status} code={code} {msg} ({path})")
        self.status = status
        self.code = code
        self.msg = msg
        self.path = path


class ServerError(ExchangeError):
    """HTTP 5xx 또는 -1007/-1006 - 처리 여부를 알 수 없다."""

    def __init__(self, status: int, code: Optional[int], msg: str, path: str = ""):
        super().__init__(f"HTTP {status} code={code} {msg} ({path})")
        self.status = status
        self.code = code
        self.msg = msg


class OrderStatusUnknown(ExchangeError):
    """주문 전송 결과를 모른다. 반드시 조회로 확정해야 한다."""

    def __init__(self, client_id: str, cause: Exception):
        super().__init__(f"주문 상태 불명 {client_id}: {cause}")
        self.client_id = client_id
        self.cause = cause


class LiveOrderBlocked(ExchangeError):
    """LiveOrderGate 가 주문(또는 계정 변경)을 거부했다. 요청은 전송되지 않았다."""


class ContractResolutionError(ExchangeError):
    """거래 가능한 BTC 무기한 계약을 확인하지 못했다 -> 거래 중지."""


# ---- 자주 쓰는 오류 코드 (Binance Derivatives) ----
CODE_UNKNOWN = -1000
CODE_DISCONNECTED = -1001
CODE_TOO_MANY_REQUESTS = -1003
CODE_UNEXPECTED_RESP = -1006
CODE_TIMEOUT = -1007            # 백엔드 응답 대기 시간 초과 - 실행 여부 불명
CODE_TIMESTAMP = -1021          # recvWindow 밖 -> 시간 재동기화 후 재시도 가능 (실행 안 됨)
CODE_INVALID_SIGNATURE = -1022
CODE_CANCEL_REJECTED = -2011
CODE_NO_SUCH_ORDER = -2013
CODE_BAD_API_KEY = -2014
CODE_REJECTED_MBX_KEY = -2015
CODE_MARGIN_INSUFFICIENT = -2019
CODE_IMMEDIATE_TRIGGER = -2021  # 조건부 주문이 즉시 발동될 가격
CODE_REDUCE_ONLY_REJECT = -2022
CODE_NO_NEED_MARGIN_TYPE = -4046
CODE_NO_NEED_POSITION_SIDE = -4059
CODE_POSITION_SIDE_MISMATCH = -4061
CODE_STOP_ORDER_SWITCH_ALGO = -4120   # 조건부 주문은 /dapi/v1/algoOrder 로
CODE_LISTEN_KEY_NOT_EXIST = -1125

UNKNOWN_STATE_CODES = {CODE_UNKNOWN, CODE_DISCONNECTED, CODE_UNEXPECTED_RESP, CODE_TIMEOUT}


def is_not_found(exc: Exception) -> bool:
    return isinstance(exc, BinanceAPIError) and exc.code in (CODE_NO_SUCH_ORDER,)


def outcome_unknown(exc: Exception) -> bool:
    """변경 요청(주문 등)에서 이 예외가 나면 실행 여부를 알 수 없다."""
    if isinstance(exc, (RequestTimeout, ServerError)):
        return True
    if isinstance(exc, NetworkError):
        return exc.maybe_sent
    if isinstance(exc, BinanceAPIError) and exc.code in UNKNOWN_STATE_CODES:
        return True
    return False
