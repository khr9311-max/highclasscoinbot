import logging
from collections import deque, defaultdict
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class BreakerResult:
    """어떤 조건이 왜 걸렸는지 알림에 그대로 실어보내기 위한 결과 객체."""

    __slots__ = ("triggered", "reasons", "z_t", "kappa_t", "betti_0")

    def __init__(self, triggered: bool, reasons: List[str], z_t: float,
                 kappa_t: float, betti_0: int):
        self.triggered = triggered
        self.reasons = reasons
        self.z_t = z_t
        self.kappa_t = kappa_t
        self.betti_0 = betti_0

    def __bool__(self) -> bool:
        return self.triggered

    def describe(self) -> str:
        detail = f"z={self.z_t:.3f}, kappa={self.kappa_t:.3f}, betti0={self.betti_0}"
        return f"{' / '.join(self.reasons)} ({detail})" if self.reasons else detail


class CircuitBreaker:
    """
    Topological & Geometric Circuit Breaker.

    기존 구현의 문제:
      1) 세 조건을 AND 로 묶어 5만 틱 시뮬레이션에서 발동 0회
      2) Betti-0 임계값(>5)이 gap_threshold=mean+2σ 구조상 도달 불가능
      3) Ricci 대용치가 정의상 항상 음수여서 신호가 아니라 상수였음

    변경:
      - 기본 동작을 OR 로 바꾸고(required=1), 몇 개 이상 걸릴 때 차단할지
        required 로 조절 가능하게 함
      - Betti-0 를 '호가 사다리의 중앙값 갭 대비 배수'로 판정해 실제로 도달 가능
      - 임계값을 고정하지 않고 종목별 자기 이력의 중앙값 + margin 으로 잡음
        (종목마다 평상시 Betti-0 가 1~10 으로 크게 다르기 때문)
      - 단일 스냅샷 스파이크를 거르기 위해 betti_persist 틱 연속 조건 추가
      - kappa 는 fisher_geometry 의 스펙트럼 엔트로피 기반(-1~+1) 값을 받음
    """

    def __init__(
        self,
        z_star: float = 0.8,
        kappa_star: float = -0.3,
        betti_0_star: int = 3,
        betti_margin: int = 5,
        betti_persist: int = 3,
        gap_multiple: float = 3.0,
        baseline_len: int = 600,
        warmup: int = 60,
        required: int = 1,
    ):
        self.z_star = z_star              # 점성 게이트 임계값
        self.kappa_star = kappa_star      # 곡률 임계값 (이 아래로 내려가면 위험)
        self.betti_0_star = betti_0_star    # Betti-0 절대 하한
        self.betti_margin = betti_margin    # 자기 기준선 대비 허용 초과폭
        self.betti_persist = max(1, betti_persist)  # 몇 틱 연속 지속돼야 인정할지
        self.gap_multiple = gap_multiple    # 중앙값 갭의 몇 배를 '단절'로 볼지
        self.baseline_len = baseline_len
        self.warmup = warmup
        self.required = max(1, required)    # 몇 개 조건이 동시에 걸리면 차단할지

        # ------------------------------------------------------------------
        # 실측 보정 (업비트 라이브 호가창, 종목당 약 3,800 표본):
        #   KRW-BTC : 중앙값 10, p90 13, p99 13, 최대 21
        #   KRW-ETH : 중앙값  1, 최대 2
        #   KRW-XRP : 중앙값  1, 최대 1
        #   KRW-SOL : 중앙값  1, 최대 1
        # BTC 는 호가 단위가 커서 빈 가격대를 건너뛰므로 평상시에도 Betti-0 가
        # 10 전후로 나온다. 고정 임계값을 쓰면 BTC 에서 상시 오발동하므로
        # 종목별 자기 이력의 중앙값과 비교한다.
        #
        # margin 별 단일틱 오발동률 (BTC 실측): +3 -> 0.26%, +5 -> 0.10%
        # 1초 틱 기준 0.26% 는 약 6분마다 1회라 매매 중단 트리거로는 너무 잦다.
        # margin=5 + 연속 3틱 지속 조건으로 순간 스파이크를 걸러낸다.
        # ------------------------------------------------------------------
        self._betti_hist = defaultdict(lambda: deque(maxlen=self.baseline_len))
        self._betti_streak = defaultdict(int)

    # ---------------- 개별 조건 ----------------
    def check_viscosity_gate(self, z_t: float) -> bool:
        """점성 게이트: z_t > z_star 이면 위험."""
        return z_t > self.z_star

    def check_ricci_curvature(self, kappa_t: float) -> bool:
        """
        곡률이 kappa_star 아래로 떨어지면 피셔 매니폴드가 저차원으로
        붕괴 중 = 구조적 위험.
        """
        return kappa_t < self.kappa_star

    def compute_betti_0(self, orderbook_depth: np.ndarray) -> int:
        """
        호가 가격 사다리의 연결성분 개수.
        인접 호가 간 갭이 '평상시 갭(중앙값)의 gap_multiple 배'를 넘으면
        그 지점을 단절로 보고, 단절 개수 + 1 을 Betti-0 로 센다.

        기존 구현은 임계값을 mean+2σ 로 잡아 20개 점에서 초과가 최대 1~2개,
        즉 betti_0 이 3을 넘을 수 없어 판정이 항상 False 였다.
        """
        depth = np.asarray(orderbook_depth, dtype=float)
        if depth.size < 3:
            return 1

        gaps = np.abs(np.diff(depth))
        gaps = gaps[np.isfinite(gaps)]
        if gaps.size == 0:
            return 1

        median_gap = float(np.median(gaps))
        if median_gap <= 0:
            positive = gaps[gaps > 0]
            if positive.size == 0:
                return 1
            median_gap = float(np.median(positive))

        breaks = int(np.sum(gaps > self.gap_multiple * median_gap))
        return breaks + 1

    def betti_threshold(self, ticker: str) -> Optional[float]:
        """해당 종목의 현재 판정 임계값. 워밍업 전이면 None."""
        hist = self._betti_hist[ticker]
        if len(hist) < self.warmup:
            return None
        baseline = float(np.median(np.fromiter(hist, dtype=float)))
        return max(float(self.betti_0_star), baseline + self.betti_margin)

    def _observe_betti(self, betti_0: int, ticker: str) -> Tuple[bool, Optional[float]]:
        """
        Betti-0 관측치를 이력에 넣고 판정한다.
        워밍업(기본 60표본) 전에는 판정하지 않는다 - 기동 직후 기준선 없이
        오발동하는 것을 막기 위함. 임계 초과가 betti_persist 틱 연속으로
        이어질 때만 True.
        """
        self._betti_hist[ticker].append(betti_0)
        threshold = self.betti_threshold(ticker)

        if threshold is None:
            self._betti_streak[ticker] = 0
            return False, None

        if betti_0 > threshold:
            self._betti_streak[ticker] += 1
        else:
            self._betti_streak[ticker] = 0

        return self._betti_streak[ticker] >= self.betti_persist, threshold

    def check_topology_betti(self, orderbook_depth: np.ndarray,
                             ticker: str = "default") -> bool:
        fired, _ = self._observe_betti(self.compute_betti_0(orderbook_depth), ticker)
        return fired

    # ---------------- 종합 판정 ----------------
    def evaluate(
        self,
        z_t: float,
        kappa_t: float,
        orderbook_depth: np.ndarray,
        ticker: str = "default",
        extra_reasons: Optional[List[str]] = None,
    ) -> BreakerResult:
        reasons: List[str] = list(extra_reasons or [])

        if self.check_viscosity_gate(z_t):
            reasons.append(f"점성게이트 활성 (z={z_t:.3f} > {self.z_star})")
        if self.check_ricci_curvature(kappa_t):
            reasons.append(f"곡률 붕괴 (kappa={kappa_t:.3f} < {self.kappa_star})")

        betti_0 = self.compute_betti_0(orderbook_depth)
        betti_fired, threshold = self._observe_betti(betti_0, ticker)
        if betti_fired:
            reasons.append(
                f"호가창 단절 (Betti-0={betti_0} > 기준 {threshold:.0f}, "
                f"{self._betti_streak[ticker]}틱 연속)"
            )

        triggered = len(reasons) >= self.required
        return BreakerResult(triggered, reasons, z_t, kappa_t, betti_0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cb = CircuitBreaker()

    normal = np.arange(100.0, 130.0, 1.0)              # 균일한 호가 사다리
    broken = np.array([100, 101, 102, 140, 141, 190, 191, 250.0])  # 구멍 뚫린 사다리

    print("정상 호가 Betti-0 :", cb.compute_betti_0(normal))
    print("단절 호가 Betti-0 :", cb.compute_betti_0(broken))
    print()

    print("워밍업 전 판정:", cb.evaluate(0.2, 0.5, broken, "KRW-TEST").describe())
    for _ in range(cb.warmup):
        cb.evaluate(0.2, 0.5, normal, "KRW-TEST")      # 기준선 학습
    print("기준선 임계값:", cb.betti_threshold("KRW-TEST"))
    print()

    print("평온       :", cb.evaluate(0.2, 0.5, normal, "KRW-TEST").describe())

    # 순간 스파이크는 무시, 지속되면 발동
    wide = np.array([100.0, 101, 102, 500, 501, 900, 901, 1400, 1401,
                     2000, 2001, 2700, 2701, 3500, 3501, 4400])
    for i in range(1, cb.betti_persist + 1):
        r = cb.evaluate(0.2, 0.5, wide, "KRW-TEST")
        print(f"단절 {i}틱째 : triggered={r.triggered}  {r.describe()}")

    print("점성 급등  :", cb.evaluate(0.95, 0.5, normal, "KRW-TEST").describe())
    print("곡률 붕괴  :", cb.evaluate(0.2, -0.7, normal, "KRW-TEST").describe())
