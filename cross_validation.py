import logging

import numpy as np
import pandas as pd
import scipy.stats as stats
from sklearn.model_selection import KFold

logger = logging.getLogger(__name__)


class PurgedKFold(KFold):
    """
    Purged-Embargo Cross-Validation (PECV)
    시계열 라벨이 겹치는 구간을 학습셋에서 제거(purge)하고, 테스트 직후
    구간을 금수(embargo)하여 정보 누수를 차단한다.
    """

    def __init__(self, n_splits=3, t1=None, pct_embargo=0.01):
        if not isinstance(t1, pd.Series):
            raise ValueError("t1 must be a pandas Series")
        super().__init__(n_splits, shuffle=False)
        self.t1 = t1
        self.pct_embargo = pct_embargo

    def split(self, X, y=None, groups=None):
        # 길이가 다르면 아래 비교가 브로드캐스트 에러를 내므로 먼저 거른다.
        if len(X) != len(self.t1):
            raise ValueError(
                f"X({len(X)})와 t1({len(self.t1)})의 길이가 다릅니다."
            )
        if not X.index.equals(self.t1.index):
            raise ValueError("X and t1 must have the same index")

        indices = np.arange(X.shape[0])
        mbrg = int(X.shape[0] * self.pct_embargo)
        test_starts = [
            (i[0], i[-1] + 1) for i in np.array_split(np.arange(X.shape[0]), self.n_splits)
        ]

        for i, j in test_starts:
            t0 = self.t1.index[i]
            test_indices = indices[i:j]
            max_t1_idx = self.t1.index.searchsorted(self.t1.iloc[test_indices].max())
            train_indices = self.t1.index.searchsorted(self.t1[self.t1 <= t0].index)

            if max_t1_idx < X.shape[0]:
                train_indices = np.concatenate((train_indices, indices[max_t1_idx + mbrg:]))

            yield train_indices, test_indices


class BacktestMetrics:
    @staticmethod
    def calculate_dsr(
        returns: np.ndarray,
        num_trials: int,
        variance_of_trials: float,
    ) -> float:
        """
        Deflated Sharpe Ratio (Bailey & López de Prado, 2014).

            DSR = Z[ (SR - SR*) * sqrt(T-1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2) ]

        기존 구현은 분모의 왜도(g3)/첨도(g4) 보정을 통째로 빼고
        (SR - SR*) * sqrt(T-1) 만 썼다. 수익률 분포의 비정규성을 반영하는 것이
        DSR 의 핵심인데 그게 빠지면 그냥 t-검정과 다를 바가 없다.

        [스케일 주의]
        variance_of_trials 는 '시행별 샤프지수의 분산'이며, returns 와 **같은
        주기 기준**(연율화하지 않은 관측치 단위)이어야 한다. 관측치 단위 SR 이
        0.1 수준인데 variance_of_trials 에 연율화 값(예: 1.5)을 넣으면 SR* 가
        SR 보다 10배 커져 DSR 이 항상 0 으로 나온다. 실제로 기존 자체 테스트가
        `DSR: 0.0` 을 뱉던 이유가 이것이다.
        """
        r = np.asarray(returns, dtype=float)
        r = r[np.isfinite(r)]
        T = r.size
        if T < 3:
            logger.warning("DSR: 관측치가 너무 적습니다 (T=%d)", T)
            return float("nan")
        if num_trials < 2:
            raise ValueError("num_trials 는 2 이상이어야 합니다.")
        if variance_of_trials <= 0:
            raise ValueError("variance_of_trials 는 0보다 커야 합니다.")

        sd = r.std(ddof=1)
        if sd <= 0:
            return float("nan")
        sr = r.mean() / sd

        g3 = float(stats.skew(r, bias=False))
        g4 = float(stats.kurtosis(r, fisher=False, bias=False))   # 정규분포 = 3

        # 스케일 불일치를 조용히 넘기면 DSR 이 항상 0/1 로 굳는다.
        sr_scale = np.sqrt(variance_of_trials)
        if abs(sr) > 0 and (sr_scale > abs(sr) * 50 or sr_scale < abs(sr) / 50):
            logger.warning(
                "DSR 스케일 의심: SR=%.4f 인데 sqrt(variance_of_trials)=%.4f 입니다. "
                "둘의 주기(관측치 단위 vs 연율화)가 같은지 확인하세요.",
                sr, sr_scale,
            )

        em_gamma = 0.5772156649  # Euler-Mascheroni
        max_sr_expected = sr_scale * (
            (1 - em_gamma) * stats.norm.ppf(1 - 1.0 / num_trials)
            + em_gamma * stats.norm.ppf(1 - 1.0 / (num_trials * np.e))
        )

        denom_sq = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr**2
        if denom_sq <= 0:
            logger.warning("DSR: 분산 보정항이 음수 - 정규 근사로 대체합니다.")
            denom_sq = 1.0

        z = (sr - max_sr_expected) * np.sqrt(T - 1) / np.sqrt(denom_sq)
        return float(stats.norm.cdf(z))

    @staticmethod
    def calculate_pbo(
        performance_matrix: np.ndarray,
        n_splits: int = 16,
        mc_sims: int = 100,
        rng: np.random.Generator = None,
    ) -> float:
        """
        Probability of Backtest Overfitting (CSCV 근사).
        performance_matrix: (N_trials, T_observations)

        수정 내용:
          기존 코드의 `np.argsort(oos_sharpe)[best_is_idx]` 는
          'best_is_idx 번째로 낮은 원소의 인덱스'이지 'best_is_idx 원소의 순위'가
          아니다. 순위를 얻으려면 argsort 를 한 번 더 하거나 rankdata 를 써야 한다.
          이 상태로는 PBO 가 사실상 무의미한 숫자였다.
        """
        mat = np.asarray(performance_matrix, dtype=float)
        if mat.ndim != 2:
            raise ValueError("performance_matrix 는 (N_trials, T) 2차원이어야 합니다.")

        N, T = mat.shape
        if N < 2:
            raise ValueError("PBO 계산에는 2개 이상의 전략(trial)이 필요합니다.")
        if n_splits % 2 != 0:
            raise ValueError("n_splits 는 짝수여야 합니다.")
        if T < n_splits * 2:
            logger.warning("PBO: 관측치 부족 (T=%d < %d)", T, n_splits * 2)
            return float("nan")

        rng = rng or np.random.default_rng()
        split_size = T // n_splits
        half = n_splits // 2
        is_overfit_count = 0

        for _ in range(mc_sims):
            perm = rng.permutation(n_splits)
            is_idx, oos_idx = perm[:half], perm[half:]

            is_ret = np.hstack([mat[:, k * split_size:(k + 1) * split_size] for k in is_idx])
            oos_ret = np.hstack([mat[:, k * split_size:(k + 1) * split_size] for k in oos_idx])

            is_sharpe = is_ret.mean(axis=1) / (is_ret.std(axis=1) + 1e-8)
            oos_sharpe = oos_ret.mean(axis=1) / (oos_ret.std(axis=1) + 1e-8)

            best_is_idx = int(np.argmax(is_sharpe))
            # 0 = 최하위. rankdata 로 '그 전략의 OOS 순위'를 정확히 얻는다.
            oos_rank = stats.rankdata(oos_sharpe)[best_is_idx] - 1.0

            # IS 1등이 OOS 에서 하위 50% 로 떨어지면 오버피팅으로 집계
            if oos_rank < (N - 1) / 2.0:
                is_overfit_count += 1

        return is_overfit_count / mc_sims


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    dates = pd.date_range(start="2023-01-01", periods=100)
    X = pd.DataFrame(np.random.randn(100, 5), index=dates)
    t1 = pd.Series(dates[1:], index=dates[:-1])
    t1.loc[dates[-1]] = dates[-1]

    cv = PurgedKFold(n_splits=3, t1=t1, pct_embargo=0.05)
    for train, test in cv.split(X):
        print("Train size:", len(train), "Test size:", len(test))

    rng = np.random.default_rng(42)
    print()
    print("--- DSR ---")
    # variance_of_trials 는 '시행별 SR 의 분산'을 returns 와 같은 주기로 넣어야 한다.
    # 여기서는 무작위 전략 200개를 돌려 실제로 측정한다.
    n_trials, T = 200, 1000
    trial_srs = []
    for _ in range(n_trials):
        x = rng.normal(0.0, 0.01, T)
        trial_srs.append(x.mean() / x.std(ddof=1))
    var_trials = float(np.var(trial_srs, ddof=1))
    print(f"측정된 시행간 SR 분산: {var_trials:.6f} (SR 표준편차 {np.sqrt(var_trials):.4f})")

    skilled = rng.normal(0.0012, 0.01, T)     # 관측치 SR 약 0.12 = 진짜 알파
    lucky = rng.normal(0.0, 0.01, T)          # 알파 없음
    print("진짜 알파 전략 DSR :", round(BacktestMetrics.calculate_dsr(skilled, n_trials, var_trials), 4))
    print("운 좋았던 전략 DSR :", round(BacktestMetrics.calculate_dsr(lucky, n_trials, var_trials), 4))

    print()
    print("--- PBO (무작위 = 실력 없음, 기대값 약 0.5) ---")
    print("PBO:", BacktestMetrics.calculate_pbo(rng.standard_normal((50, 500)), rng=rng))
