import json
import logging
import os
import time
from collections import deque, defaultdict
from typing import Dict, List, Optional, Tuple

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
      - Betti-0 기준선을 디스크에 영속화 (아래 참고)

    2026-09-18 라이브 보정 (새벽 5시간 15분간 52회 오발동):
      - 위상(Betti-0) 조건 기본 비활성화 (topology_enabled=False).
        임계값 문제가 아니라 지표가 감지하려는 사건에 반응하지 못한다.
        실측 BTC 사다리(60단계)에서 호가를 실제로 취소해가며 측정한 결과:

            호가 취소   0%   40%   60%   80%   95%
            Betti-0      7     6     5     3     1

        유동성이 사라질수록 값이 내려간다. 호가창이 95% 증발한 상태가
        Betti-0=1, 즉 '완벽히 연결된 정상 호가창'으로 판정된다.
        compute_betti_0 이 갭을 '같은 스냅샷의 중앙값 갭'으로 정규화하기
        때문이다 - 호가가 사라지면 모든 갭이 같이 커져 비율이 유지되고,
        점 개수까지 줄어 셀 수 있는 갭 자체가 없어진다(3단계만 남으면
        갭이 2개라 Betti-0 는 최대 3).
        정규화 기준을 이력 갭으로 바꿔도 7->9 까지만 오르다 다시 3 으로
        떨어진다. '남은 단계 사이의 간격 불규칙성'을 재는 지표로는
        '단계가 없어지는 것'을 감지할 수 없다 - 도구가 맞지 않는다.

        그동안 발동한 것은 전부 노이즈였다. 프로덕션 33분 실측 분포는
        중앙값 7 / p90 13 / p99 16 / 최대 19 이고, 자기상관이 lag1 0.82 로
        높아 '10틱 연속 초과'가 드문 사건이 아니다. betti_margin/
        betti_persist 를 올리는 것은 이 노이즈 대역 안에서 문턱만 옮기는
        일이라, 발동 빈도는 줄지만 감지 능력은 생기지 않는다.
        (실측: margin+5/persist10 -> 2.4회/시, margin+8/persist10 -> 1.8회/시)

        재설계한다면 연결성분이 아니라 단계 개수 / 총 잔량 / 가격 스팬을
        직접 보는 쪽이어야 한다. 그때까지는 z_t 가 이 역할을 한다 -
        rel_spread 가 들어 있어 호가가 증발하면 스프레드가 벌어져 반응한다.
        betti0 기록은 계속 쌓으므로(_observe_betti 는 계속 호출) 재설계용
        데이터는 끊기지 않는다.

        betti_margin 8 / betti_persist 10 은 조건을 다시 켤 때를 대비해
        남겨둔 값이다. 노이즈를 줄이긴 하지만 그것만으로는 부족하다.
      - 곡률 조건 기본 비활성화 (curvature_enabled=False).
        kappa 는 표준화된 피처 상관행렬의 스펙트럼 엔트로피라, 값이 낮다는
        것은 "피처가 한 방향으로 몰렸다" = 호가가 얇고 한산하다는 뜻이다.
        위험이 아니라 한산함을 재고 있어 부호가 뒤집혀 있다. 실측 음수 비율:
        BTC 2.5% / ETH 10.1% / XRP 37.5% / SOL 49.2% (1,499틱 기준).
        임계값만 내리면 증상만 가려지므로 지표 재정의 전까지 끈다.
        ※ 종목별 평가로 확장할 때 이걸 켜둔 채로 가면 SOL/XRP 가 상시 발동
          상태가 된다. 두 변경은 반드시 함께 간다.
      - 발동 시 연속 카운터 리셋 (_observe_betti 참고).
    """

    def __init__(
        self,
        z_star: float = 0.8,
        kappa_star: float = -0.3,
        betti_0_star: int = 3,
        betti_margin: int = 8,
        betti_persist: int = 10,
        gap_multiple: float = 3.0,
        baseline_len: int = 600,
        warmup: int = 60,
        required: int = 1,
        curvature_enabled: bool = False,
        topology_enabled: bool = False,
        state_path: Optional[str] = None,
        baseline_max_age_sec: float = 6 * 3600.0,
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
        self.curvature_enabled = curvature_enabled
        self.topology_enabled = topology_enabled

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
        self._betti_hist: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.baseline_len))
        self._betti_streak: Dict[str, int] = defaultdict(int)

        # ------------------------------------------------------------------
        # 기준선 영속화.
        # 이 이력이 메모리에만 있으면 프로세스 재시작마다 초기화되어, 재기동
        # 직후 몇 분간은 '하루치 정상 분포'가 아니라 '방금 쌓인 몇 분치'로
        # 임계값을 잡는다. 실제로 운영 중 재시작을 반복하다가 재기동 3분여
        # 만에 비교적 흔한 수준(Betti-0=18, 과거 실측 최대치 21 이내)의
        # 스파이크에 서킷브레이커가 걸린 사례가 있었다 - 신호 자체는 진짜였지만
        # 기준선이 덜 여물어 margin 이 평소보다 타이트했다.
        # 그래서 종료 시 이력을 저장하고, 시작 시 너무 오래되지 않았으면
        # (baseline_max_age_sec 이내) 복원한다.
        # ------------------------------------------------------------------
        self.state_path = state_path
        self.baseline_max_age_sec = baseline_max_age_sec
        if self.state_path:
            self._load_baseline()

    # ---------------- 영속화 ----------------
    def _load_baseline(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Betti-0 기준선 로딩 실패(새로 시작): %s", e)
            return

        age = time.time() - data.get("saved_at", 0)
        if age > self.baseline_max_age_sec:
            logger.info(
                "저장된 Betti-0 기준선이 %.1f시간 전 것이라 폐기하고 새로 시작합니다.",
                age / 3600.0,
            )
            return

        restored = 0
        for ticker, values in data.get("hist", {}).items():
            self._betti_hist[ticker] = deque(values, maxlen=self.baseline_len)
            restored += 1
        if restored:
            logger.info(
                "Betti-0 기준선 복원: %d개 종목 (%.1f분 전 저장분)",
                restored, age / 60.0,
            )

    def save_baseline(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            data = {
                "saved_at": time.time(),
                "hist": {k: list(v) for k, v in self._betti_hist.items() if v},
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.error("Betti-0 기준선 저장 실패: %s", e)

    # ---------------- 개별 조건 ----------------
    def check_viscosity_gate(self, z_t: float) -> bool:
        """점성 게이트: z_t > z_star 이면 위험."""
        return z_t > self.z_star

    def check_ricci_curvature(self, kappa_t: float) -> bool:
        """
        곡률이 kappa_star 아래로 떨어지면 피셔 매니폴드가 저차원으로
        붕괴 중 = 구조적 위험.

        기본 비활성(curvature_enabled=False). 이유는 클래스 docstring 참고.
        재정의 전까지는 켜지 않는다. 켜려면 curvature_enabled=True.
        """
        if not self.curvature_enabled:
            return False
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

    def _observe_betti(self, betti_0: int,
                       ticker: str) -> Tuple[bool, Optional[float], int]:
        """
        Betti-0 관측치를 이력에 넣고 판정한다.
        워밍업(기본 60표본) 전에는 판정하지 않는다 - 기동 직후 기준선 없이
        오발동하는 것을 막기 위함. 임계 초과가 betti_persist 틱 연속으로
        이어질 때만 True.

        발동이 선 순간 연속 카운터를 0으로 되돌린다. 리셋하지 않으면 단절이
        지속되는 동안 카운터가 계속 누적돼, main.py 의 60초 쿨다운이 풀리는
        족족 같은 사건으로 재발동한다. 리셋 후에는 betti_persist 틱을 처음부터
        다시 채워야 발동하므로, 한 사건은 최소 (쿨다운 + persist)틱 간격을 둔다.
        """
        self._betti_hist[ticker].append(betti_0)
        threshold = self.betti_threshold(ticker)

        if threshold is None:
            self._betti_streak[ticker] = 0
            return False, None, 0

        if betti_0 > threshold:
            self._betti_streak[ticker] += 1
        else:
            self._betti_streak[ticker] = 0

        streak = self._betti_streak[ticker]
        if streak >= self.betti_persist:
            self._betti_streak[ticker] = 0
            return True, threshold, streak
        return False, threshold, streak

    def check_topology_betti(self, orderbook_depth: np.ndarray,
                             ticker: str = "default") -> bool:
        """
        기본 비활성(topology_enabled=False). 이유는 클래스 docstring 참고.
        이력 적재는 계속 하므로(_observe_betti 호출은 유지) 지표를 재설계할
        때 쓸 데이터는 끊기지 않는다.
        """
        fired, _, _ = self._observe_betti(self.compute_betti_0(orderbook_depth), ticker)
        return fired and self.topology_enabled

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
        # 비활성일 때도 _observe_betti 는 계속 호출한다. 판정만 막고 이력은
        # 쌓아야 지표 재설계용 데이터가 끊기지 않는다.
        betti_fired, threshold, streak = self._observe_betti(betti_0, ticker)
        if betti_fired and self.topology_enabled:
            reasons.append(
                f"호가창 단절 (Betti-0={betti_0} > 기준 {threshold:.0f}, "
                f"{streak}틱 연속)"
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

    # 곡률은 기본 비활성. 켰을 때만 반응해야 한다.
    off = cb.evaluate(0.2, -0.7, normal, "KRW-TEST")
    print(f"곡률 붕괴(기본 off): triggered={off.triggered}  {off.describe()}")
    assert not off.triggered, "곡률이 기본 비활성이어야 한다"

    cb_k = CircuitBreaker(curvature_enabled=True)
    on = cb_k.evaluate(0.2, -0.7, normal, "KRW-TEST")
    print(f"곡률 붕괴(on)      : triggered={on.triggered}  {on.describe()}")
    assert on.triggered, "켜면 곡률 단독으로 발동해야 한다"

    # 영속화 라운드트립 검증
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cb_state.json")
        cb2 = CircuitBreaker(state_path=path)
        for _ in range(cb2.warmup):
            cb2.evaluate(0.2, 0.5, normal, "KRW-PERSIST")
        thr_before = cb2.betti_threshold("KRW-PERSIST")
        cb2.save_baseline()

        cb3 = CircuitBreaker(state_path=path)
        thr_after = cb3.betti_threshold("KRW-PERSIST")
        print()
        print(f"영속화 테스트: 저장 전 임계값={thr_before} / 재기동 직후(로딩) 임계값={thr_after}")
        assert thr_after is not None and thr_after == thr_before, "기준선 복원 실패"
        print("영속화 OK - 재시작 후에도 워밍업 없이 바로 판정 가능")
