"""
clientOrderId / clientAlgoId 규칙 - 중복 주문 방지의 기반.

  cm1<거래id 10자>-<역할><순번>     예) cm1a3f9c2e71b-EN0, cm1a3f9c2e71b-SL2, cm1a3f9c2e71b-TP1_0

  역할: EN 진입, SL 보호 손절, TP 목표(뒤에 단계), EX 청산, EM 비상 청산
  같은 역할의 재시도는 순번을 올린다. '결과를 모르는' 주문은 같은 id 로 조회해서
  확정하기 전에는 절대 새 순번으로 다시 보내지 않는다 (execution/context.py).

바이낸스 규칙: ^[\\.A-Z\\:/a-z0-9_-]{1,36}$ . 이 봇이 만든 주문은 접두사 cm1 로 구분한다
(사람이 낸 주문·다른 봇의 주문은 건드리지 않는다).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

PREFIX = "cm1"
ROLES = ("EN", "SL", "TP", "EX", "EM")
_RE = re.compile(r"^cm1([0-9a-f]{10})-(?:(TP)(\d)_(\d+)|(EN|SL|EX|EM)(\d+))$")
_VALID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


def make(trade_id: str, role: str, seq: int, level: Optional[int] = None) -> str:
    if role not in ROLES:
        raise ValueError(role)
    body = f"{role}{level}_{seq}" if role == "TP" else f"{role}{seq}"
    cid = f"{PREFIX}{trade_id}-{body}"
    if not _VALID.match(cid):
        raise ValueError(f"잘못된 clientOrderId: {cid}")
    return cid


def parse(cid: str) -> Optional[Tuple[str, str, Optional[int], int]]:
    """(trade_id, role, level, seq) 또는 None (이 봇의 주문이 아님)."""
    m = _RE.match(cid or "")
    if not m:
        return None
    if m.group(2) == "TP":
        return m.group(1), "TP", int(m.group(3)), int(m.group(4))
    return m.group(1), m.group(5), None, int(m.group(6))


def is_ours(cid: str) -> bool:
    return parse(cid) is not None


def trade_of(cid: str) -> Optional[str]:
    p = parse(cid)
    return p[0] if p else None
