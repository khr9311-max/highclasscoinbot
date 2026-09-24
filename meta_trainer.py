"""
메타 레이블링 모델 자동 학습기.

data_recorder 가 쌓은 (1) 가격 시계열과 (2) LLM 1차 신호를 짝지어
삼중장벽으로 라벨을 만들고 LightGBM 메타 모델을 학습한다.

메타 레이블링의 핵심은 '1차 모델의 신호가 맞았는가'를 2차로 판정하는 것이므로,
라벨은 방향 예측(1/-1)이 아니라 성공 여부(1/0)다.

검증은 PurgedKFold 로 한다. 일반 KFold 를 쓰면 삼중장벽의 라벨 구간이
학습셋과 겹쳐 성능이 부풀려진다.
"""

import os
import re
import json
import glob
import pickle
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 삼중장벽 기본 설정
DEFAULT_HORIZON_SEC = 1800.0   # 수직 장벽 30분
DEFAULT_PT_MULT = 2.0          # 익절 = horizon 변동성 x 2
DEFAULT_SL_MULT = 2.0          # 손절 = horizon 변동성 x 2
VOL_LOOKBACK_SEC = 3600.0      # 변동성 추정 구간 1시간

# 시장가 왕복 마찰비용. 업비트 KRW 마켓 수수료 0.05% x 2 + 스프레드 크로싱
# 약 0.02% (BTC 실측). 장벽이 이보다 작으면 '성공' 라벨이 붙어도 실제로는
# 손실이므로, 메타 모델이 수익성과 무관한 것을 학습하게 된다.
ROUND_TRIP_COST = 0.0012
MIN_BARRIER_MULT = 2.0         # 장벽 하한 = 왕복비용 x 2


class MetaTrainer:
    def __init__(
        self,
        data_dir: str,
        model_path: str,
        min_samples: int = 300,
        horizon_sec: float = DEFAULT_HORIZON_SEC,
        pt_mult: float = DEFAULT_PT_MULT,
        sl_mult: float = DEFAULT_SL_MULT,
    ):
        self.data_dir = data_dir
        self.model_path = model_path
        self.min_samples = min_samples
        self.horizon_sec = horizon_sec
        self.pt_mult = pt_mult
        self.sl_mult = sl_mult

    # ------------------------------------------------------------------
    # 적재 데이터 로딩
    # ------------------------------------------------------------------
    def load_prices(self, ticker: str) -> Optional[pd.DataFrame]:
        """prices/*.parquet 과 *.csv 를 모두 읽어 시각순 시리즈로 합친다."""
        frames = []
        pdir = os.path.join(self.data_dir, "prices")
        for path in sorted(glob.glob(os.path.join(pdir, "*"))):
            try:
                if path.endswith(".parquet"):
                    df = pd.read_parquet(path, columns=["ts", "ticker", "mid"])
                elif path.endswith(".csv"):
                    df = pd.read_csv(path, usecols=["ts", "ticker", "mid"])
                elif path.endswith(".csv.gz"):
                    df = pd.read_csv(path, usecols=["ts", "ticker", "mid"], compression="gzip")
                else:
                    continue
                frames.append(df[df["ticker"] == ticker])
            except Exception as e:
                logger.warning("가격 파일 로딩 실패 (%s): %s", os.path.basename(path), e)

        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        out = out.dropna(subset=["ts", "mid"]).sort_values("ts").reset_index(drop=True)
        out = out[out["mid"] > 0]
        return out if len(out) else None

    def load_signals(self) -> List[Dict[str, Any]]:
        sigs = []
        sdir = os.path.join(self.data_dir, "signals")
        for path in sorted(glob.glob(os.path.join(sdir, "*.jsonl"))):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            sigs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning("신호 파일 로딩 실패 (%s): %s", os.path.basename(path), e)
        sigs.sort(key=lambda r: r.get("ts", 0))
        return sigs

    # ------------------------------------------------------------------
    # 삼중장벽 라벨링
    # ------------------------------------------------------------------
    @staticmethod
    def _ewma_vol(prices: np.ndarray) -> float:
        """구간 로그수익률의 표준편차. 관측 1개(=1틱) 기준이다."""
        if len(prices) < 5:
            return 0.0
        r = np.diff(np.log(prices))
        r = r[np.isfinite(r)]
        return float(np.std(r)) if r.size else 0.0

    def _horizon_vol(self, ts_seg: np.ndarray, px_seg: np.ndarray) -> float:
        """
        관측당 변동성을 horizon(수직 장벽) 스케일로 환산한다.

        이 환산이 빠져 있던 것이 라벨링의 핵심 버그였다. _ewma_vol 은 관측
        1개당 std 를 주는데, 그 값을 진입 후 '누적' 수익률과 비교하고 있었다.
        삼중장벽 방법론(López de Prado)은 일봉 기준이라 '1개 바'와 'horizon'
        이 같은 자릿수여서 pt = 2*vol 이 자연스럽게 맞지만, 1초 틱에 30분
        horizon 이면 sqrt(1800) ≈ 42배 차이가 난다.

        실측(2026-09-18, BTC 16.8시간):
            1초 std          0.0000471   -> 2*std = 0.0092%
            왕복 마찰비용                        0.12%
            즉 장벽이 손익분기점의 0.08배라, '성공' 라벨이 붙은 거래도
            수수료를 내면 전부 손실이었다.

        같은 파일의 폴백 상수 0.002 가 sqrt 환산값(0.001999)과 정확히 일치한다
        - 의도는 horizon 스케일이었는데 본 계산만 관측 스케일로 남아 있었다.

        샘플링 주기는 고정으로 보지 않고 실제 간격의 중앙값을 쓴다. 기록
        데이터가 보통 1초지만 최대 11초까지 벌어지는 구간이 있다.
        """
        per_obs = self._ewma_vol(px_seg)
        if per_obs <= 0 or len(ts_seg) < 2:
            return 0.0
        dt = float(np.median(np.diff(np.asarray(ts_seg, dtype=float))))
        if not np.isfinite(dt) or dt <= 0:
            return 0.0
        n_obs = max(1.0, self.horizon_sec / dt)
        return per_obs * float(np.sqrt(n_obs))

    def label_signal(
        self, ts: float, side: int, ts_arr: np.ndarray, px_arr: np.ndarray
    ) -> Optional[Tuple[int, float, str]]:
        """
        한 신호에 삼중장벽을 적용한다.
        반환: (메타라벨 0/1, 실현수익률, 어느 장벽에 닿았는지)
        아직 수직 장벽이 지나지 않았으면 None (라벨 확정 불가).
        """
        i = int(np.searchsorted(ts_arr, ts, side="left"))
        if i >= len(ts_arr) - 1:
            return None

        entry = float(px_arr[i])
        if entry <= 0:
            return None

        # 진입 직전 구간으로 변동성 추정 (horizon 스케일)
        lo = int(np.searchsorted(ts_arr, ts - VOL_LOOKBACK_SEC, side="left"))
        vol = self._horizon_vol(ts_arr[lo:i + 1], px_arr[lo:i + 1])
        if vol <= 0:
            vol = 0.002   # 30분 스케일 대체값 (BTC 실측 ~0.2%)

        # 장벽이 왕복 마찰비용보다 작으면 '성공' 이 실제 수익을 뜻하지 않는다.
        barrier = max(self.pt_mult * vol, MIN_BARRIER_MULT * ROUND_TRIP_COST)
        pt = barrier
        sl = -barrier * (self.sl_mult / self.pt_mult)
        deadline = ts + self.horizon_sec

        # 수직 장벽이 아직 안 지났으면 라벨을 확정할 수 없다
        if ts_arr[-1] < deadline:
            return None

        j_end = int(np.searchsorted(ts_arr, deadline, side="right"))
        path = px_arr[i:j_end]
        if len(path) < 2:
            return None

        rets = (path / entry - 1.0) * side   # 방향 반영

        hit_pt = np.argmax(rets >= pt) if np.any(rets >= pt) else -1
        hit_sl = np.argmax(rets <= sl) if np.any(rets <= sl) else -1

        if hit_pt >= 0 and (hit_sl < 0 or hit_pt <= hit_sl):
            return 1, float(rets[hit_pt]), "pt"
        if hit_sl >= 0:
            return 0, float(rets[hit_sl]), "sl"

        final = float(rets[-1])
        return (1 if final > 0 else 0), final, "vertical"

    # ------------------------------------------------------------------
    # 데이터셋 구성
    # ------------------------------------------------------------------
    def build_dataset(self) -> Optional[Tuple[pd.DataFrame, pd.Series, pd.Series]]:
        signals = self.load_signals()
        if not signals:
            logger.info("적재된 신호가 없습니다.")
            return None

        actionable = [s for s in signals if s.get("action") in ("BUY", "SELL")]
        logger.info("신호 %d건 (그중 BUY/SELL %d건)", len(signals), len(actionable))
        if not actionable:
            return None

        by_ticker: Dict[str, List[Dict]] = {}
        for s in actionable:
            by_ticker.setdefault(s.get("ticker", "?"), []).append(s)

        rows, labels, t0s, t1s = [], [], [], []
        for ticker, sigs in by_ticker.items():
            prices = self.load_prices(ticker)
            if prices is None or len(prices) < 100:
                logger.warning("%s 가격 데이터 부족 - 건너뜀", ticker)
                continue
            ts_arr = prices["ts"].to_numpy(dtype=float)
            px_arr = prices["mid"].to_numpy(dtype=float)

            for s in sigs:
                side = 1 if s["action"] == "BUY" else -1
                res = self.label_signal(float(s["ts"]), side, ts_arr, px_arr)
                if res is None:
                    continue
                label, ret, barrier = res

                feats = s.get("features") or []
                if not feats:
                    continue
                row = list(map(float, feats))
                # 1차 모델의 자체 확신도도 메타 모델의 입력으로 넣는다
                row += [
                    float(s.get("confidence", 0.0)),
                    float(s.get("crypto_score", 0.0)),
                    float(s.get("news_score", 0.0)),
                    float(side),
                ]
                rows.append(row)
                labels.append(label)
                t0s.append(float(s["ts"]))
                t1s.append(float(s["ts"]) + self.horizon_sec)

        if not rows:
            logger.info("라벨 확정 가능한 표본이 없습니다 (수직 장벽 미도래 포함).")
            return None

        n_feat = len(rows[0])
        cols = [f"f{i}" for i in range(n_feat - 4)] + ["confidence", "crypto_score", "news_score", "side"]

        # PurgedKFold 는 '이벤트 시작시각을 인덱스, 종료시각을 값'으로 갖는
        # t1 시리즈를 요구한다. 두 시각이 같은 시간축에 있어야 겹침 판정이 된다.
        order = np.argsort(t0s)
        idx = pd.to_datetime(np.asarray(t0s)[order], unit="s")
        # 같은 초에 두 신호가 들어오면 인덱스가 중복돼 정렬/검색이 깨진다
        idx = pd.DatetimeIndex(idx)
        if idx.has_duplicates:
            bump = pd.to_timedelta(
                pd.Series(np.arange(len(idx))).groupby(idx.astype("int64")).cumcount().to_numpy(),
                unit="ms",
            )
            idx = idx + bump

        X = pd.DataFrame(np.asarray(rows)[order], columns=cols, index=idx)
        y = pd.Series(np.asarray(labels)[order], name="meta_label", index=idx)
        t1 = pd.Series(pd.to_datetime(np.asarray(t1s)[order], unit="s"), name="t1", index=idx)
        return X, y, t1

    # ------------------------------------------------------------------
    # 학습
    # ------------------------------------------------------------------
    def train_if_ready(self) -> Dict[str, Any]:
        ds = self.build_dataset()
        if ds is None:
            return {"trained": False, "reason": "표본 없음"}

        X, y, t1 = ds
        n = len(X)
        pos = int(y.sum())

        if n < self.min_samples:
            return {"trained": False, "reason": f"표본 부족 ({n}/{self.min_samples})", "n": n}
        if pos == 0 or pos == n:
            return {"trained": False, "reason": f"한쪽 클래스만 존재 (양성 {pos}/{n})", "n": n}

        import lightgbm as lgb
        from sklearn.metrics import roc_auc_score

        # ---- Purged K-Fold 검증 (라벨 구간 겹침 제거) ----
        # 일반 KFold 를 쓰면 삼중장벽 구간이 학습셋과 겹쳐 AUC 가 부풀려진다.
        auc_scores, skipped = [], []
        try:
            from cross_validation import PurgedKFold
            cv = PurgedKFold(n_splits=3, t1=t1, pct_embargo=0.02)
            for tr, te in cv.split(X):
                if len(tr) < 30 or len(te) < 10:
                    skipped.append(f"표본부족(train={len(tr)},test={len(te)})")
                    continue
                if y.iloc[tr].nunique() < 2 or y.iloc[te].nunique() < 2:
                    skipped.append("단일클래스")
                    continue
                m = lgb.LGBMClassifier(n_estimators=150, num_leaves=15,
                                       min_child_samples=10, verbose=-1)
                m.fit(X.iloc[tr], y.iloc[tr])
                auc_scores.append(roc_auc_score(y.iloc[te], m.predict_proba(X.iloc[te])[:, 1]))
        except Exception as e:
            logger.warning("Purged CV 실패 (학습은 계속): %s", e)

        if not auc_scores:
            logger.warning("Purged CV 유효 폴드 없음 - 사유: %s", skipped or "알 수 없음")
        cv_auc = float(np.mean(auc_scores)) if auc_scores else None

        # ---- 전체 데이터로 최종 학습 ----
        model = lgb.LGBMClassifier(n_estimators=150, num_leaves=15,
                                   min_child_samples=10, verbose=-1)
        model.fit(X, y)

        meta = {
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "n_samples": n,
            "n_positive": pos,
            "positive_rate": round(pos / n, 4),
            "cv_auc": round(cv_auc, 4) if cv_auc is not None else None,
            "feature_names": list(X.columns),
            "horizon_sec": self.horizon_sec,
            "pt_mult": self.pt_mult,
            "sl_mult": self.sl_mult,
        }

        os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
        tmp = self.model_path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"model": model, "meta": meta}, f)
        os.replace(tmp, self.model_path)

        logger.info(
            "메타 모델 학습 완료: 표본 %d건 (양성률 %.1f%%) CV AUC=%s -> %s",
            n, meta["positive_rate"] * 100,
            meta["cv_auc"] if meta["cv_auc"] is not None else "N/A",
            self.model_path,
        )
        return {"trained": True, **meta}

    # ------------------------------------------------------------------
    @staticmethod
    def load_model(model_path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(model_path):
            return None
        try:
            with open(model_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.error("메타 모델 로딩 실패: %s", e)
            return None


class NakedMetaTrainer:
    """
    가격행동 전략 전용 메타 모델.

    1차 신호는 패턴(price_action)이고, 라벨은 그 신호의 종이 매매 결과다
    (비용 차감 순수익 > 0 이면 1). LLM 메타 모델과 달리 삼중장벽을 따로
    만들지 않는다 - 패턴이 손절(꼬리 아래)과 목표(다음 존)를 이미 정해 주므로
    그게 곧 구조적 장벽이다.

    표본은 청산 방식 하나(운용 중인 NAKED_EXIT_MODE)로만 쓴다. 같은 신호를 청산 방식 4가지로
    굴린 종이 거래를 전부 넣으면 한 신호가 4번 들어가 CV 가 부풀려진다.
    백테스트 거래(naked/backtest_trades.jsonl)도 표본으로 쓴다.
    """

    def __init__(self, data_dir: str, model_path: str, variant: str = "zone",
                 min_samples: int = 200):
        self.data_dir = data_dir
        self.model_path = model_path
        self.variant = variant
        self.min_samples = min_samples

    def load_trades(self) -> List[Dict[str, Any]]:
        base = os.path.join(self.data_dir, "naked")
        paths = sorted(glob.glob(os.path.join(base, "trades", "*.jsonl")))
        bt = os.path.join(base, "backtest_trades.jsonl")
        if os.path.exists(bt):
            paths.append(bt)
        out = []
        for p in paths:
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (r.get("filled") and r.get("status") == "closed"
                                and r.get("variant") == self.variant
                                and r.get("source", "paper") in ("paper", "backtest")):
                            out.append(r)
            except Exception as e:
                logger.warning("가격행동 거래 로딩 실패 (%s): %s", os.path.basename(p), e)
        return out

    def build_dataset(self):
        from naked_strategy import feature_row, feature_names
        trades = self.load_trades()
        if not trades:
            return None
        # 같은 신호가 백테스트와 종이 매매에 둘 다 있으면 하나만
        seen, rows, labels, t0s, t1s = set(), [], [], [], []
        for r in trades:
            key = (r["ticker"], r["signal_ts"], r["pattern"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(feature_row(r["signal"], bool(r.get("is_alt"))))
            labels.append(1 if float(r.get("net_ret", 0.0)) > 0 else 0)
            t0s.append(float(r["signal_ts"]))
            t1s.append(float(r.get("exit_ts") or r["signal_ts"]))
        order = np.argsort(t0s)
        idx = pd.DatetimeIndex(pd.to_datetime(np.asarray(t0s)[order], unit="s"))
        if idx.has_duplicates:
            bump = pd.to_timedelta(
                pd.Series(np.arange(len(idx))).groupby(idx.astype("int64")).cumcount().to_numpy(),
                unit="ms",
            )
            idx = idx + bump
        X = pd.DataFrame(np.asarray(rows)[order], columns=feature_names(), index=idx)
        y = pd.Series(np.asarray(labels)[order], name="meta_label", index=idx)
        t1 = pd.Series(pd.to_datetime(np.asarray(t1s)[order], unit="s"), name="t1", index=idx)
        # 종료 시각이 시작보다 앞일 수 없다 (같은 봉 청산은 같은 시각)
        t1 = t1.where(t1.values >= idx.values, pd.Series(idx, index=idx))
        return X, y, t1

    def train_if_ready(self) -> Dict[str, Any]:
        ds = self.build_dataset()
        if ds is None:
            return {"trained": False, "reason": "가격행동 거래 표본 없음"}
        X, y, t1 = ds
        n, pos = len(X), int(y.sum())
        if n < self.min_samples:
            return {"trained": False, "reason": f"표본 부족 ({n}/{self.min_samples})", "n": n}
        if pos == 0 or pos == n:
            return {"trained": False, "reason": f"한쪽 클래스만 존재 (양성 {pos}/{n})", "n": n}

        import lightgbm as lgb
        from sklearn.metrics import roc_auc_score
        from cross_validation import PurgedKFold

        aucs = []
        try:
            cv = PurgedKFold(n_splits=4, t1=t1, pct_embargo=0.02)
            for tr, te in cv.split(X):
                if len(tr) < 40 or len(te) < 15:
                    continue
                if y.iloc[tr].nunique() < 2 or y.iloc[te].nunique() < 2:
                    continue
                m = lgb.LGBMClassifier(n_estimators=120, num_leaves=7,
                                       min_child_samples=15, verbose=-1)
                m.fit(X.iloc[tr], y.iloc[tr])
                aucs.append(roc_auc_score(y.iloc[te], m.predict_proba(X.iloc[te])[:, 1]))
        except Exception as e:
            logger.warning("가격행동 메타 CV 실패 (학습은 계속): %s", e)
        cv_auc = float(np.mean(aucs)) if aucs else None

        model = lgb.LGBMClassifier(n_estimators=120, num_leaves=7,
                                   min_child_samples=15, verbose=-1)
        model.fit(X, y)
        meta = {
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "n_samples": n, "n_positive": pos, "positive_rate": round(pos / n, 4),
            "cv_auc": round(cv_auc, 4) if cv_auc is not None else None,
            "cv_folds": len(aucs), "feature_names": list(X.columns),
            "variant": self.variant,
        }
        os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
        tmp = self.model_path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"model": model, "meta": meta}, f)
        os.replace(tmp, self.model_path)
        logger.info("가격행동 메타 모델: 표본 %d · 양성률 %.1f%% · CV AUC %s",
                    n, meta["positive_rate"] * 100, meta["cv_auc"])
        return {"trained": True, **meta}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("COINBOT_STATE_DIR", "state")
    model_path = os.path.join(data_dir, "meta_model.pkl")

    trainer = MetaTrainer(data_dir, model_path)
    result = trainer.train_if_ready()
    print(json.dumps(result, ensure_ascii=False, indent=2))
