"""
메타 레이블링 학습 데이터 적재기.

기존 파이프라인은 메모리 deque 에만 데이터를 두고 재시작하면 전부 날렸다.
메타 모델을 훈련시키려면 (1) 삼중장벽 판정용 가격 시계열과
(2) 1차 신호 시점의 특징 벡터가 디스크에 남아 있어야 한다.

저장 전략:
  - 낮 동안은 CSV/JSONL 로 append (프로세스가 죽어도 그때까지 기록이 남는다)
  - 자정에 Parquet 으로 압축 후 원본 삭제 (용량 5~7배 절감)
  - 보존 기간이 지난 파일은 자동 삭제

용량: 4종목 1초 스냅샷 기준 CSV 약 14MB/일 -> Parquet 약 2~3MB/일.
"""

import os
import io
import csv
import json
import time
import glob
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class DataRecorder:
    """
    가격 스냅샷 / LLM 신호 / 주문 결과를 디스크에 적재한다.

    매 틱마다 파일 I/O 를 때리면 이벤트 루프가 느려지므로 메모리에
    버퍼링했다가 주기적으로 flush 한다.
    """

    PRICE_HEADER = ["ts", "ticker", "mid", "last", "spread", "book_imb", "flow_imb", "vol", "visc"]

    def __init__(
        self,
        base_dir: str,
        flush_interval: float = 30.0,
        flush_rows: int = 300,
        retention_days: int = 180,
    ):
        self.base = base_dir
        self.price_dir = os.path.join(base_dir, "prices")
        self.signal_dir = os.path.join(base_dir, "signals")
        self.order_dir = os.path.join(base_dir, "orders")
        for d in (self.price_dir, self.signal_dir, self.order_dir):
            os.makedirs(d, exist_ok=True)

        self.flush_interval = flush_interval
        self.flush_rows = flush_rows
        self.retention_days = retention_days

        self._price_buf: List[List[Any]] = []
        self._last_flush = time.monotonic()
        self._current_day = self._today()
        self._lock = threading.Lock()

        self.rows_written = 0
        self.signals_written = 0

    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now(KST).strftime("%Y-%m-%d")

    def _price_path(self, day: str) -> str:
        return os.path.join(self.price_dir, f"{day}.csv")

    def _signal_path(self, day: str) -> str:
        return os.path.join(self.signal_dir, f"{day}.jsonl")

    def _order_path(self, day: str) -> str:
        return os.path.join(self.order_dir, f"{day}.jsonl")

    # ------------------------------------------------------------------
    def record_prices(self, market_state, tickers: List[str]):
        """매 틱 호출. 버퍼에만 쌓고 실제 쓰기는 flush 에서 한다."""
        now = time.time()
        rows = []
        for t in tickers:
            st = market_state.get(t)
            if not st or not st.last_price or not st.asks or not st.bids:
                continue
            rows.append([
                round(now, 3), t,
                st.mid_price, st.last_price,
                round(st.rel_spread(), 8),
                round(st.book_imbalance(), 6),
                round(st.flow_imbalance(), 6),
                round(st.realized_vol(), 8),
                round(st.viscosity(), 6),
            ])
        if not rows:
            return

        with self._lock:
            self._price_buf.extend(rows)
            should_flush = (
                len(self._price_buf) >= self.flush_rows
                or time.monotonic() - self._last_flush >= self.flush_interval
            )
        if should_flush:
            self.flush()

    def record_signal(
        self,
        ticker: str,
        features: np.ndarray,
        action: str,
        confidence: float,
        crypto_score: float,
        news_score: float,
        price: float,
        executed: bool,
        reason: str = "",
        ts: Optional[float] = None,
    ):
        """
        LLM 이 1차 신호를 낸 시점을 기록한다. 메타 모델의 학습 표본이 되는
        지점이므로 실행 여부(executed)와 무관하게 전부 남긴다.

        ts 는 과거 데이터 백필/테스트용 override 다. 운영 중에는 넘기지 않는다.
        """
        rec = {
            "ts": round(ts if ts is not None else time.time(), 3),
            "ticker": ticker,
            "action": action,
            "confidence": float(confidence),
            "crypto_score": float(crypto_score),
            "news_score": float(news_score),
            "price": float(price) if price else None,
            "executed": bool(executed),
            "reason": (reason or "")[:300],
            "features": [round(float(x), 6) for x in np.asarray(features).ravel()],
        }
        try:
            with open(self._signal_path(self._today()), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.signals_written += 1
        except Exception as e:
            logger.error("신호 기록 실패: %s", e)

    def record_order(self, order_uuid: str, ticker: str, side: str,
                     price: float, amount: float, state: str = "submitted",
                     extra: Optional[Dict[str, Any]] = None):
        rec = {
            "ts": round(time.time(), 3),
            "uuid": order_uuid,
            "ticker": ticker,
            "side": side,
            "price": float(price) if price else None,
            "amount": float(amount) if amount else None,
            "state": state,
        }
        if extra:
            rec.update(extra)
        try:
            with open(self._order_path(self._today()), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("주문 기록 실패: %s", e)

    # ------------------------------------------------------------------
    def flush(self):
        with self._lock:
            if not self._price_buf:
                self._last_flush = time.monotonic()
                return
            buf, self._price_buf = self._price_buf, []
            self._last_flush = time.monotonic()

        day = self._today()
        path = self._price_path(day)
        try:
            new_file = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(self.PRICE_HEADER)
                w.writerows(buf)
            self.rows_written += len(buf)
        except Exception as e:
            logger.error("가격 기록 flush 실패: %s", e)

    # ------------------------------------------------------------------
    def maintain(self):
        """
        자정에 날짜가 바뀌면 전날 CSV 를 Parquet 으로 압축하고,
        보존 기간이 지난 파일을 지운다. 하루 1회 호출하면 충분하다.
        """
        today = self._today()
        if today == self._current_day:
            return False

        prev = self._current_day
        self._current_day = today
        self.flush()

        self._compact(prev)
        self._purge_old()
        return True

    def _compact(self, day: str):
        """전날 CSV -> Parquet. pyarrow 가 없으면 gzip 으로 대체한다."""
        src = self._price_path(day)
        if not os.path.exists(src):
            return
        try:
            import pandas as pd
            df = pd.read_csv(src)
            dst = os.path.join(self.price_dir, f"{day}.parquet")
            df.to_parquet(dst, compression="snappy", index=False)
            before = os.path.getsize(src)
            after = os.path.getsize(dst)
            os.remove(src)
            logger.info(
                "가격 데이터 압축 %s: %.1fMB -> %.1fMB (%.1f배)",
                day, before / 1e6, after / 1e6, before / max(after, 1),
            )
        except ImportError:
            import gzip, shutil
            with open(src, "rb") as fi, gzip.open(src + ".gz", "wb") as fo:
                shutil.copyfileobj(fi, fo)
            os.remove(src)
            logger.warning("pyarrow 없음 - gzip 으로 압축했습니다 (%s)", day)
        except Exception as e:
            logger.error("압축 실패 (%s): %s", day, e)

    def _purge_old(self):
        cutoff = datetime.now(KST) - timedelta(days=self.retention_days)
        removed = 0
        for d in (self.price_dir, self.signal_dir, self.order_dir):
            for path in glob.glob(os.path.join(d, "*")):
                name = os.path.basename(path).split(".")[0]
                try:
                    if datetime.strptime(name, "%Y-%m-%d").replace(tzinfo=KST) < cutoff:
                        os.remove(path)
                        removed += 1
                except ValueError:
                    continue
        if removed:
            logger.info("보존기간(%d일) 경과 파일 %d개 삭제", self.retention_days, removed)

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        def dirsize(d):
            return sum(os.path.getsize(p) for p in glob.glob(os.path.join(d, "*")) if os.path.isfile(p))

        n_signals = 0
        for p in glob.glob(os.path.join(self.signal_dir, "*.jsonl")):
            try:
                with open(p, encoding="utf-8") as f:
                    n_signals += sum(1 for _ in f)
            except Exception:
                pass

        total = dirsize(self.price_dir) + dirsize(self.signal_dir) + dirsize(self.order_dir)
        days = len(glob.glob(os.path.join(self.price_dir, "*")))
        return {
            "누적_신호": n_signals,
            "적재_일수": days,
            "총_용량_MB": round(total / 1e6, 2),
            "일평균_MB": round(total / 1e6 / max(days, 1), 2),
            "세션_기록행": self.rows_written,
        }

    def close(self):
        self.flush()
