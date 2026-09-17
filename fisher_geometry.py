import logging

import numpy as np
import ot  # POT (Python Optimal Transport)

logger = logging.getLogger(__name__)


class FisherGeometry:
    def __init__(self, window_size: int = 60, epsilon: float = 1e-8):
        self.window_size = window_size
        self.epsilon = epsilon

    def compute_fisher_information(self, gradients: np.ndarray) -> np.ndarray:
        """
        Fisher Information Manifold (G_t) = 1/T * sum(g g^T)

        기존 구현은 파이썬 for 루프로 외적을 누적했는데, 매 틱 호출되는
        경로라 행렬곱 한 번으로 대체한다 (동일 결과, 수십 배 빠름).
        """
        grads = np.asarray(gradients, dtype=float)
        if grads.size == 0:
            return np.array([])
        if grads.ndim == 1:
            grads = grads.reshape(1, -1)

        recent = grads[-self.window_size:]
        T, num_params = recent.shape

        G_t = (recent.T @ recent) / max(T, 1)
        G_t += np.eye(num_params) * self.epsilon   # 수치 안정화
        return G_t

    def compute_geodesic_slippage(
        self, G_t: np.ndarray, theta_t: np.ndarray, theta_next: np.ndarray
    ) -> float:
        """
        Geodesic Slippage S*.
        피셔 계량 하의 마할라노비스 거리로 측지선 거리를 근사한다.
        """
        if G_t.size == 0:
            return 0.0

        diff = (np.asarray(theta_next, dtype=float) - np.asarray(theta_t, dtype=float)).reshape(-1, 1)
        if diff.shape[0] != G_t.shape[0]:
            logger.warning(
                "theta 차원(%d)과 G_t 차원(%d) 불일치 - 슬리피지 0 처리",
                diff.shape[0], G_t.shape[0],
            )
            return 0.0

        dist_sq = float((diff.T @ G_t @ diff).item())
        return float(np.sqrt(max(dist_sq, 0.0)))

    def compute_wasserstein_dissipation(
        self, p_calm: np.ndarray, p_turb: np.ndarray, cost_matrix: np.ndarray, reg: float = 0.1
    ) -> float:
        """
        Sinkhorn 기반 Wasserstein-2 수송 비용.

        기존 코드는 `except ot.utils.StopError` 를 잡았는데 POT 에 그런 예외가
        없어서, 실제로 예외가 나면 AttributeError 로 원인이 가려졌다.
        """
        p_calm = np.ascontiguousarray(p_calm, dtype=float)
        p_turb = np.ascontiguousarray(p_turb, dtype=float)

        s_calm, s_turb = p_calm.sum(), p_turb.sum()
        if s_calm <= 0 or s_turb <= 0:
            return 0.0
        p_calm, p_turb = p_calm / s_calm, p_turb / s_turb

        try:
            W_t = ot.sinkhorn2(p_calm, p_turb, np.ascontiguousarray(cost_matrix, dtype=float), reg)
            W_t = float(np.asarray(W_t).ravel()[0])
            if not np.isfinite(W_t):
                return 0.0
            return W_t
        except Exception as e:
            logger.warning("Sinkhorn 수렴 실패 - 수송비용 0 처리: %s", e)
            return 0.0

    def get_ricci_scalar_curvature(self, G_t: np.ndarray) -> float:
        """
        서킷브레이커용 곡률 대용치. 범위 [-1, +1].

        기존 구현 `mean(eig) - max(eig)` 는 정의상 **항상 <= 0** 이라
        "음수면 위험" 판정이 상수 True 가 되어 신호 역할을 못 했다
        (난수 2000회 검증에서 음수 비율 100%).

        대체 지표: 피셔 행렬 고유값 분포의 정규화 스펙트럼 엔트로피 H.
          H -> 1  : 등방적 = 잘 조건화된 매니폴드 (안전)
          H -> 0  : 한 방향으로 붕괴 = 저차원 퇴화 (위험)
        kappa = 2H - 1 로 옮기면 '퇴화 중일 때만 음수'가 되어
        원래 의도했던 "곡률 음수 = 구조적 붕괴"가 실제로 성립한다.
        """
        if G_t.size == 0:
            return 0.0

        eigenvalues = np.linalg.eigvalsh(G_t)
        eigenvalues = np.clip(eigenvalues, 0.0, None)

        total = eigenvalues.sum()
        n = eigenvalues.size
        if total <= 0 or n < 2:
            return -1.0

        p = eigenvalues / total
        p = p[p > 0]
        if p.size < 2:
            return -1.0

        H = float(-(p * np.log(p)).sum() / np.log(n))
        H = min(1.0, max(0.0, H))
        return float(2.0 * H - 1.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fg = FisherGeometry()

    healthy = fg.compute_fisher_information(np.random.randn(100, 5))
    print("Fisher Information Matrix Shape:", healthy.shape)

    t1 = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    t2 = np.array([0.15, 0.2, 0.25, 0.4, 0.5])
    print("Geodesic Slippage:", fg.compute_geodesic_slippage(healthy, t1, t2))

    n = 10
    M = ot.dist(np.arange(n).reshape(-1, 1), np.arange(n).reshape(-1, 1), metric="sqeuclidean")
    print("Wasserstein Dissipation:", fg.compute_wasserstein_dissipation(ot.unif(n), ot.unif(n), M))

    # 곡률 지표가 실제로 부호를 갖는지 확인
    base = np.random.randn(100, 5)
    degenerate = np.outer(np.random.randn(100), np.random.randn(5))  # rank-1 붕괴
    print()
    print("정상 그래디언트 kappa :", round(fg.get_ricci_scalar_curvature(healthy), 4))
    print("퇴화(rank-1)  kappa :",
          round(fg.get_ricci_scalar_curvature(fg.compute_fisher_information(degenerate)), 4))
