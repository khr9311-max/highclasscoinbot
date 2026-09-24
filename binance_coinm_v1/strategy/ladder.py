"""
청산 규칙 (Naked Forex 11장) - 방향 일반화 + V1 래더(부분청산).

원본 Trade.on_bar_close 의 규칙을 롱/숏 대칭으로 옮겼다. '더 유리한 손절' 은
롱이면 더 높은 값, 숏이면 더 낮은 값이다 (손절은 한 방향으로만 움직인다).

변형 (백테스트 시행 집합. 실거래 V1 은 ladder 만):
  ladder         - V1 기본. TP1/TP2/TP3 에서 부분청산(설정 비율) + 원본 사다리 손절 +
                   목표를 다 지나면(또는 목표가 없으면) 3봉 추적
  ladder_ratchet - 원본 업비트 ladder 그대로 (부분청산 없음, 사다리 손절 + 추적)
  zone           - 첫 존에서 전량 (존 없으면 3봉 추적)
  split          - 첫 존 50% + 본전 손절, 나머지는 두 번째 존 (없으면 추적)
  three_bar      - 처음부터 3봉 추적

사다리 손절(원본): 봉 마감 때 그 봉 고가(숏은 저가)가 k 번째 목표에 닿았으면
  k=1 -> 손절을 진입가(본전)로, k=2 -> TP1 으로, k=3 -> TP2 로 올린다.
3봉 추적(원본): 보유 3봉째부터 최근 3봉 저가 - 0.05 ATR (숏은 고가 + 0.05 ATR).
봉 마감 청산 순서(원본): 반전 신호 -> (V1 추가) 반대 방향 진입 신호 -> 보유 기한 -> 종가가 손절선 밖.

부분청산 수량은 '누적 내림' 으로 정한다: TPk 까지의 누적 청산 = floor(초기수량 x 누적비율).
그래서 합이 초기 수량을 넘지 않고, 남은 수량은 정확히 정수 계약이다 (계약이 적으면
앞쪽 TP 의 부분청산이 0 이 되어 건너뛰어진다 - 새 포지션을 만들 일은 없다).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .price_action import Bars, atr

EXIT_VARIANTS = ("ladder", "ladder_ratchet", "zone", "split", "three_bar")
TRAIL_BARS = 3
BUF_ATR = 0.05


def better_stop(direction: int, current: float, new: float) -> float:
    return max(current, new) if direction > 0 else min(current, new)


def tp_schedule(variant: str, n_targets: int,
                fractions: Sequence[float] = (0.25, 0.25, 0.25)) -> List[Tuple[int, float]]:
    """[(목표 인덱스, 초기 수량 대비 비율)]."""
    if variant == "ladder":
        return [(k, float(fractions[k])) for k in range(min(3, n_targets)) if fractions[k] > 0]
    if variant == "zone":
        return [(0, 1.0)] if n_targets >= 1 else []
    if variant == "split":
        if n_targets >= 2:
            return [(0, 0.5), (1, 0.5)]
        return [(0, 0.5)] if n_targets == 1 else []
    return []


def tp_quantities(initial_qty: Any, schedule: Sequence[Tuple[int, float]],
                  step: Any = 1) -> List[Tuple[int, Decimal]]:
    """누적 내림 방식 부분청산 수량. 0 인 단계는 빼고 돌려준다."""
    q0 = Decimal(str(initial_qty))
    st = Decimal(str(step))
    out: List[Tuple[int, Decimal]] = []
    cum_frac = Decimal(0)
    done = Decimal(0)
    for level, frac in schedule:
        cum_frac += Decimal(repr(float(frac)))
        target = (q0 * min(cum_frac, Decimal(1)) / st).to_integral_value(rounding=ROUND_DOWN) * st
        q = target - done
        if q > 0:
            out.append((level, q))
            done = target
    return out


@dataclass
class BarDecision:
    new_stop: Optional[float] = None          # 손절을 옮겨야 하면 새 값
    exit_reason: Optional[str] = None         # 봉 마감 즉시 청산 사유
    ladder_step: int = 0
    trailing: bool = False


@dataclass
class PositionLogic:
    """보유 중 거래의 봉 마감 판정. 네트워크·수량과 무관한 순수 규칙."""
    direction: int
    fill_price: float
    stop: float
    targets: List[float]
    variant: str = "ladder"
    max_hold_bars: int = 72
    bars_held: int = 0
    ladder_step: int = 0
    trailing_active: bool = False
    tp_filled: List[int] = field(default_factory=list)

    def __post_init__(self):
        if self.variant not in EXIT_VARIANTS:
            raise ValueError(f"알 수 없는 청산 방식: {self.variant}")

    @property
    def uses_ladder(self) -> bool:
        return self.variant in ("ladder", "ladder_ratchet")

    def trailing_rule(self) -> bool:
        if self.variant == "three_bar":
            return True
        if self.variant in ("zone", "split"):
            if not self.targets:
                return True
            if self.variant == "split" and 0 in self.tp_filled and len(self.targets) < 2:
                return True
        return False

    def on_tp_filled(self, level: int) -> Optional[float]:
        """부분청산 체결 직후. split 은 첫 목표 뒤 즉시 본전 손절(원본 mark_exit)."""
        if level not in self.tp_filled:
            self.tp_filled.append(level)
        if self.variant == "split" and level == 0:
            new = better_stop(self.direction, self.stop, self.fill_price)
            if new != self.stop:
                self.stop = new
                return new
        return None

    def _trail_level(self, bars: Bars, i: int, buf: float) -> float:
        lo = max(0, i - TRAIL_BARS + 1)
        if self.direction > 0:
            return float(bars.l[lo:i + 1].min()) - buf
        return float(bars.h[lo:i + 1].max()) + buf

    def on_bar_close(self, bars: Bars, i: int, reversal: bool = False,
                     opposite: bool = False) -> BarDecision:
        d = self.direction
        self.bars_held += 1
        old = self.stop
        buf = BUF_ATR * atr(bars, i)
        extreme = float(bars.h[i]) if d > 0 else float(bars.l[i])

        if self.uses_ladder:
            while self.ladder_step < len(self.targets) and \
                    d * extreme >= d * self.targets[self.ladder_step]:
                new = self.fill_price if self.ladder_step == 0 else self.targets[self.ladder_step - 1]
                self.stop = better_stop(d, self.stop, float(new))
                self.ladder_step += 1
            if self.ladder_step >= len(self.targets) and self.bars_held >= TRAIL_BARS:
                self.stop = better_stop(d, self.stop, self._trail_level(bars, i, buf))
                self.trailing_active = True
        if self.trailing_rule() and self.bars_held >= TRAIL_BARS:
            self.stop = better_stop(d, self.stop, self._trail_level(bars, i, buf))
            self.trailing_active = True

        c = float(bars.c[i])
        why = None
        if reversal:
            why = "reversal_signal"
        elif opposite:
            why = "opposite_signal"
        elif self.bars_held >= self.max_hold_bars:
            why = "time"
        elif d * c <= d * self.stop:
            why = "stop_close"
        return BarDecision(new_stop=self.stop if self.stop != old else None, exit_reason=why,
                           ladder_step=self.ladder_step, trailing=self.trailing_active)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PositionLogic":
        return cls(**d)
