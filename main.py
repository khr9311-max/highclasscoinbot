import asyncio
import logging
import os
import signal
import time
from collections import deque
from typing import Optional

import numpy as np

from config import Config
from execution_engine import ExecutionEngine
from fisher_geometry import FisherGeometry
from circuit_breaker import CircuitBreaker
from multi_agent import MultiAgentSystem, decide_action
from triple_barrier import MetaLabeling
from free_energy_ppo import OnlineFreeEnergyAgent, MarketReplayBuffer, OBS_DIM
from data_recorder import DataRecorder
from meta_trainer import MetaTrainer
from news_feed import NewsFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# httpx 는 요청마다 INFO 를 찍어 실제 로그를 묻어버린다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

FORWARD_HORIZON_SEC = 60.0   # 전방수익률 측정 구간 (RL 보상용)


class Scheduler:
    """
    절대 데드라인 기반 주기 실행기.

    기존 코드의 `int(time.time()) % 300 == 0` 방식은 틱 드리프트로 초를
    건너뛰면 그 주기를 통째로 놓친다 (측정: 틱당 120ms 오버헤드에서
    24시간 288회 중 277회만 발동). 데드라인 방식은 밀려도 반드시 1회 실행된다.
    """

    def __init__(self):
        self._next = {}

    def due(self, name: str, interval: float) -> bool:
        now = time.monotonic()
        nxt = self._next.get(name)
        if nxt is None:
            # 기동 직후 전부 동시에 터지지 않도록 한 주기 뒤로 민다.
            self._next[name] = now + interval
            return False
        if now >= nxt:
            # 크게 밀린 경우 밀린 주기를 몰아서 실행하지 않되, 다음 발동은
            # 반드시 interval 이후로 민다. max(now, ...) 로 두면 _next 가 now 가
            # 되어 바로 뒤이은 호출이 또 발동한다(LLM 판단 연속 2회 등).
            self._next[name] = max(now, nxt) + interval
            return True
        return False


class MainPipeline:
    def __init__(self):
        Config.validate()

        self.engine = ExecutionEngine()
        self.mas = MultiAgentSystem()
        self.news_feed = NewsFeed()
        self.fisher = FisherGeometry(window_size=Config.WINDOW_SIZE)
        self.cb = CircuitBreaker(
            state_path=os.path.join(Config.STATE_DIR, "circuit_breaker_baseline.json")
        )
        self.meta_labeling = MetaLabeling()

        self.replay = MarketReplayBuffer()
        self.rl_agent = OnlineFreeEnergyAgent(self.replay)

        # 메타 레이블링 학습 데이터 적재 + 자동 학습
        self.recorder = DataRecorder(Config.STATE_DIR)
        # ExecutionEngine 은 recorder 보다 먼저 만들어져 생성자에서 못 받는다
        # (main.py:67 vs 여기). 주문 체결/취소를 orders/*.jsonl 에 남기려면
        # 이 연결이 필요하다 - 이게 없어서 실제 체결이 나도 로그 파일이
        # 하나도 안 쌓이는 문제가 있었다.
        self.engine.recorder = self.recorder
        self.meta_model_path = os.path.join(Config.STATE_DIR, "meta_model.pkl")
        self.meta_trainer = MetaTrainer(Config.STATE_DIR, self.meta_model_path)
        self._meta_bundle = None      # {"model":..., "meta":...}
        self._meta_mtime = 0.0

        self.scheduler = Scheduler()
        self.primary_ticker = Config.TARGET_TICKERS[0]

        # 시장 특징 벡터 이력. 피셔 정보행렬을 여기서 계산한다.
        # (기존에는 np.random.randn(5) 를 '그래디언트'라며 쌓고 있었다)
        self.feature_history = deque(maxlen=Config.WINDOW_SIZE * 2)

        # 전방수익률 계산 대기열: (시각, 관측치, 기준가)
        self._pending_obs = deque()

        self._llm_task: Optional[asyncio.Task] = None
        # create_task 결과를 붙잡아두지 않으면 GC 대상이 되어 조용히 사라지고
        # 그 안에서 난 예외도 묻힌다.
        self._bg_tasks: set = set()
        self._shutdown = asyncio.Event()
        self._cb_active_until = 0.0
        self._last_kappa = 0.0
        self._halt_notified = False

    # ------------------------------------------------------------------
    def _spawn(self, coro, name: str) -> asyncio.Task:
        """배경 태스크를 참조와 함께 띄우고, 끝나면 예외를 로그로 남긴다."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task):
            self._bg_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error("배경 태스크 [%s] 예외: %s", name, exc)

        task.add_done_callback(_done)
        return task

    def _collect_features(self, ticker: Optional[str] = None) -> Optional[np.ndarray]:
        ticker = ticker or self.primary_ticker
        st = self.engine.market.get(ticker)
        if not st or not self.engine.market.is_ready(ticker):
            return None
        return st.feature_vector(OBS_DIM)

    def _update_replay_buffer(self, obs: np.ndarray, price: float):
        """
        관측치를 대기열에 넣고, FORWARD_HORIZON_SEC 지난 것부터
        실현 전방수익률과 짝지어 리플레이 버퍼로 보낸다.
        """
        now = time.monotonic()
        self._pending_obs.append((now, obs, price))

        while self._pending_obs and now - self._pending_obs[0][0] >= FORWARD_HORIZON_SEC:
            t0, obs0, p0 = self._pending_obs.popleft()
            if p0 and p0 > 0 and price > 0:
                self.replay.push(obs0, (price / p0) - 1.0)

    def _compute_curvature(self) -> float:
        """
        시장 특징 벡터들의 경험적 정보행렬에서 곡률 지표를 뽑는다.
        값이 음수로 내려가면 시장 요인이 한 방향으로 붕괴 중이라는 뜻.
        """
        if len(self.feature_history) < 20:
            return 0.0
        feats = np.vstack(self.feature_history)
        # 스케일 차이가 큰 성분이 고유값을 지배하지 않도록 표준화
        std = feats.std(axis=0)
        std[std == 0] = 1.0
        G_t = self.fisher.compute_fisher_information((feats - feats.mean(axis=0)) / std)
        return self.fisher.get_ricci_scalar_curvature(G_t)

    # ------------------------------------------------------------------
    async def process_tick(self):
        ticker = self.primary_ticker

        ok, why = self.engine.market_ready(ticker)
        if not ok:
            if self.scheduler.due("warn_nodata", 30.0):
                logger.warning("시장 데이터 대기 중: %s", why)
            return

        st = self.engine.market.get(ticker)
        obs = self._collect_features()
        if obs is None:
            return

        self.feature_history.append(obs)
        self._update_replay_buffer(obs, st.mid_price or 0.0)

        # ---- 1. 위험 지표 산출 (전부 실측값) ----
        z_t = st.viscosity()
        if self.scheduler.due("curvature", 5.0):
            self._last_kappa = self._compute_curvature()

        # ---- 2. 하트비트 ----
        # 서킷브레이커보다 먼저 찍는다. 매매가 정지된 동안에도 프로세스가
        # 살아 있다는 신호는 계속 나가야 모니터링이 성립한다.
        if self.scheduler.due("heartbeat", 60.0):
            logger.info(
                "HEARTBEAT ticks=%d ws_msgs=%d ws_age=%.1fs replay=%d z=%.3f kappa=%.3f "
                "orders=%d cb=%s",
                self.engine.clock.tick_count, self.engine.ws_feed.message_count,
                self.engine.ws_feed.age(), len(self.replay), z_t, self._last_kappa,
                len(self.engine.active_orders),
                time.monotonic() < self._cb_active_until,
            )

        # Betti-0 기준선을 주기적으로 디스크에 저장한다. 재시작마다 기준선이
        # 사라져 얕은 데이터로 다시 워밍업하던 문제(재기동 직후 오발동 원인)
        # 를 막기 위함. 매 틱 쓰면 낭비라 1분 간격으로만.
        if self.scheduler.due("cb_baseline_save", 60.0):
            self._spawn(asyncio.to_thread(self.cb.save_baseline), "cb_baseline_save")

        # ---- 3. 서킷 브레이커 ----
        result = self.cb.evaluate(z_t, self._last_kappa, st.depth_curve(), ticker)

        # 메타 모델 학습용 가격 시계열 + 브레이커 진단값 적재.
        # 판정 뒤·early return 앞이라는 위치가 중요하다. 예전처럼 판정 앞에
        # 두면 그 틱의 betti0 을 아직 모르고, 뒤로 더 내리면 발동한 틱 - 즉
        # 정작 사후 분석이 필요한 순간 - 의 기록이 통째로 빠진다.
        # evaluate() 가 이미 이번 관측치를 이력에 넣었으므로, 여기서 읽는
        # 임계값은 방금 판정에 쓰인 것과 같다. 워밍업 중이면 None.
        cb_thr = self.cb.betti_threshold(ticker)
        self.recorder.record_prices(
            self.engine.market, Config.TARGET_TICKERS,
            diag={ticker: {
                "betti0": result.betti_0,
                "cb_thr": round(cb_thr, 2) if cb_thr is not None else "",
                "kappa": round(self._last_kappa, 6),
            }},
        )
        # 호가 사다리 원본은 저속(기본 10초)으로 별도 기록. 지표 자체를
        # 재정의할 때 필요하다. 간격이 안 찼으면 내부에서 그냥 넘어간다.
        self.recorder.record_depth(self.engine.market, Config.TARGET_TICKERS)

        if result.triggered:
            if time.monotonic() > self._cb_active_until:
                logger.critical("서킷 브레이커 발동: %s", result.describe())
                await self.engine.cancel_all()
                await self.engine.notifier.notify_circuit_breaker(result.describe())
            self._cb_active_until = time.monotonic() + 60.0   # 60초 쿨다운
            return

        if time.monotonic() < self._cb_active_until:
            return   # 쿨다운 중에는 신규 진입 금지

        # ---- 4. 리스크 회계 ----
        if self.scheduler.due("equity", 30.0):
            pnl = self.engine.risk.update_equity(self.engine.total_equity_krw())
            logger.info(
                "손익 %s원 | 노출 %s원 | %s",
                f"{pnl:+,.0f}", f"{self.engine.total_exposure_krw():,.0f}",
                self.engine.risk.summary(),
            )
            # 정지 알림은 1회만. 30초마다 같은 메시지를 반복해 보내지 않는다.
            if self.engine.risk.halted and not self._halt_notified:
                self._halt_notified = True
                await self.engine.notifier.notify_risk_halt(self.engine.risk.halt_reason)
            elif not self.engine.risk.halted:
                self._halt_notified = False

        # ---- 5. LLM 의사결정 (클럭을 막지 않도록 백그라운드 실행) ----
        if self.scheduler.due("llm", Config.LLM_INTERVAL_SEC):
            if self._llm_task and not self._llm_task.done():
                logger.warning("이전 LLM 판단이 아직 진행 중 - 이번 주기 건너뜀.")
            else:
                self._llm_task = self._spawn(self._run_llm_decision(), "llm_decision")

        # ---- 6. 온라인 학습 ----
        if self.scheduler.due("train", Config.TRAIN_INTERVAL_SEC):
            self._spawn(self.rl_agent.train_online(timesteps=64), "ppo_train")

        # ---- 7. 데이터 유지보수 + 메타 모델 재학습 (1시간마다 점검) ----
        if self.scheduler.due("data_maint", 3600.0):
            self._spawn(self._maintain_and_train(), "meta_train")

    # ------------------------------------------------------------------
    def _market_summary(self, ticker: str, st, price: float) -> str:
        return (
            f"{ticker} | 현재가 {price:,.0f} | "
            f"스프레드 {st.rel_spread()*100:.4f}% | "
            f"호가불균형 {st.book_imbalance():+.3f} | "
            f"주문흐름 {st.flow_imbalance():+.3f} | "
            f"실현변동성 {st.realized_vol()*100:.4f}% | "
            f"점성 {st.viscosity():.3f}"
        )

    async def _run_llm_decision(self):
        """
        종목별 판정. 실매매는 primary_ticker 만, 나머지는 섀도(기록만)다.

        섀도를 두는 이유: 메타 모델은 BUY/SELL 표본 300건이 있어야 학습되는데
        (meta_trainer.min_samples), BTC 단독이면 실측 4.5건/시간이라 약 3일이
        걸린다. 그렇다고 검증 안 된 신호로 4종목 실매매를 켜면 마찰비용
        (왕복 약 0.12%)과 리스크가 그대로 4배가 된다.
        meta_trainer 는 라벨을 '주문 체결 여부'가 아니라 기록된 가격 시계열의
        삼중장벽으로 만들기 때문에(executed 필드를 쓰지 않는다), 주문을 내지
        않아도 표본이 된다. 그래서 돈은 BTC 에만 걸고 데이터는 4종목에서
        모은다.

        한계: 섀도 신호에는 슬리피지/부분체결이 없어 실제보다 낙관적이다.
        특히 호가가 얇은 XRP/SOL 에서 차이가 크므로, 확장 판단 시 감안해야 한다.

        뉴스 점수는 종목과 무관하므로 한 번만 호출하고 공유한다.
        크립토 에이전트만 종목 수만큼 호출된다(동시 실행).
        """
        try:
            live_ticker = self.primary_ticker
            tickers = list(Config.TARGET_TICKERS) if Config.SHADOW_MODE else [live_ticker]

            ready = []
            for t in tickers:
                st = self.engine.market.get(t)
                if not st or not self.engine.market.is_ready(t):
                    continue
                price = st.last_price or st.mid_price or 0.0
                ready.append((t, st, price))
            if not ready:
                return

            # 뉴스도 종목별로 받는다. 공유 뉴스를 쓰던 때는 헤드라인이 BTC
            # 중심이라 알트 판정을 깎았다(SOL 부호 불일치 76%, 신호 -25%).
            headline_sets = await asyncio.gather(*(
                self.news_feed.get_headlines(t) for t, _st, _p in ready
            ))
            agent_results = await asyncio.gather(*(
                asyncio.gather(
                    self.mas.run_crypto_agent(self._market_summary(t, st, price)),
                    self.mas.run_news_agent(hl),
                )
                for (t, st, price), hl in zip(ready, headline_sets)
            ))

            for (ticker, st, price), (c_res, n_res) in zip(ready, agent_results):
                is_live = ticker == live_ticker
                # LLM 이 죽었을 때 '중립 판단'으로 착각하고 매매하지 않는다.
                if not c_res.get("ok"):
                    logger.warning("[%s] Crypto Agent 응답 실패 - 이번 주기 건너뜀.", ticker)
                    continue
                if not n_res.get("ok"):
                    # 예전 규칙은 news=0 을 '합의 실패'로 보고 무조건 막았지만,
                    # 지금은 crypto 단독 기준이 올라갈 뿐 막히지는 않는다.
                    logger.warning("[%s] News Agent 응답 실패 - crypto 단독 기준.", ticker)
                await self._decide_one(ticker, st, price, c_res.get("score", 0.0),
                                      n_res.get("score", 0.0), is_live)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("LLM 의사결정 중 예외: %s", e)
            await self.engine.notifier.notify_error(f"LLM 의사결정 예외: {e}")

    async def _decide_one(self, ticker: str, st, price: float,
                          c_score: float, n_score: float, is_live: bool):
        """한 종목의 판정 + 기록. is_live 일 때만 필터를 태우고 주문을 낸다."""
        # 최종 판정은 LLM 산문이 아니라 숫자 규칙으로 한다.
        # (multi_agent.decide_action 상단 주석 참고 - LLM 판정은 실측
        #  59건 전부 HOLD 였고, 그중 41%는 두 점수의 부호가 일치했다.)
        t_res = decide_action(c_score, n_score, Config.MIN_ENTRY_SCORE)
        action = t_res["action"]
        confidence = t_res["strength"]
        tag = "" if is_live else "[섀도] "
        logger.info(
            "%s판정 %s: %s (강도 %.2f) | crypto=%.2f news=%.2f | %s",
            tag, ticker, action, confidence, c_score, n_score, t_res["reason"],
        )

        obs = self._collect_features(ticker)

        def log_signal(executed: bool, why: str = ""):
            # HOLD 를 포함해 모든 판단을 남긴다. 나중에 메타 모델이
            # '이 신호가 실제로 통했는가'를 학습하는 표본이 된다.
            if obs is not None:
                self.recorder.record_signal(
                    ticker=ticker, features=obs, action=action,
                    confidence=confidence, crypto_score=c_score,
                    news_score=n_score, price=price,
                    executed=executed, reason=why or t_res["reason"],
                )

        if action == "HOLD":
            log_signal(False, "HOLD")
            return

        if not is_live:
            # 섀도: 표본만 남기고 필터/주문은 타지 않는다. RL 은 BTC 특징으로
            # 학습돼 있어 다른 종목에 그대로 적용하면 근거가 없고, 메타 필터도
            # 아직 학습 전이라 여기서 거르면 표본만 줄어든다.
            log_signal(False, "섀도 (기록만)")
            return

        # ---- RL 에이전트 확인 필터 ----
        # 기존 코드에서 rl_agent 는 학습만 하고 의사결정에 전혀 쓰이지 않았다.
        if obs is not None and self.replay.ready(128):
            exposure = float(np.mean(self.rl_agent.get_action(obs)))
            if (action == "BUY" and exposure < 0) or (action == "SELL" and exposure > 0):
                logger.info("RL 에이전트 반대 의견 (exposure=%.3f) - 주문 보류.", exposure)
                log_signal(False, f"RL 반대 (exposure={exposure:.3f})")
                return

        # ---- 메타 레이블링 필터 ----
        # 학습된 모델이 없으면 통과시키지 않는다. 기존 코드는 meta_prob 을
        # 0.85 로 하드코딩해 필터가 항상 열려 있었다.
        meta_prob = self._meta_probability(obs, action, confidence, c_score, n_score)
        if meta_prob is None:
            # 대체 게이트. 예전에는 LLM 이 스스로 매긴 confidence 를 0.7 로
            # 잘랐지만, 지금 confidence 는 decide_action 이 낸 강도
            # |0.7*crypto + 0.3*news| 라 스케일이 다르다. 진입 규칙이 이미
            # 같은 값(MIN_ENTRY_SCORE)으로 걸렀으므로 여기서 또 자르면
            # 이중 게이트가 된다. 더 엄격하게 가려면 이 값만 올리면 된다.
            logger.info("메타 모델 미학습 - 강도 임계로 대체 판정 (강도=%.2f)", confidence)
            if confidence < Config.META_FALLBACK_MIN_STRENGTH:
                logger.info("진입 강도 부족 - 주문 보류.")
                log_signal(False, f"강도 부족 ({confidence:.2f})")
                return
        elif meta_prob < 0.6:
            logger.info("메타 모델 필터 차단 (p=%.3f < 0.6)", meta_prob)
            log_signal(False, f"메타 필터 차단 (p={meta_prob:.3f})")
            return
        else:
            logger.info("메타 모델 통과 (p=%.3f)", meta_prob)

        if action == "BUY":
            ok = await self.engine.place_market_buy(ticker)
        else:
            ok = await self.engine.place_market_sell(ticker)
        log_signal(bool(ok))

    async def _maintain_and_train(self):
        """
        자정 경과 시 전날 CSV 를 Parquet 으로 압축하고 오래된 파일을 정리한다.
        이어서 누적 표본으로 메타 모델 재학습을 시도한다 (표본이 모자라면 건너뜀).
        둘 다 블로킹 작업이라 별도 스레드에서 돌린다.
        """
        try:
            rolled = await asyncio.to_thread(self.recorder.maintain)
            stats = self.recorder.stats()
            logger.info(
                "데이터 적재 현황 | 신호 %s건 · %s일치 · %sMB (일평균 %sMB)%s",
                stats["누적_신호"], stats["적재_일수"], stats["총_용량_MB"],
                stats["일평균_MB"], " · 일자전환+압축 완료" if rolled else "",
            )

            result = await asyncio.to_thread(self.meta_trainer.train_if_ready)
            if result.get("trained"):
                logger.info(
                    "메타 모델 갱신: 표본 %d건 · 양성률 %.1f%% · CV AUC %s",
                    result["n_samples"], result["positive_rate"] * 100,
                    result.get("cv_auc"),
                )
                await self.engine.notifier.send_message(
                    f"<b>🧠 메타 모델 학습 완료</b>\n"
                    f"표본 {result['n_samples']}건 · 양성률 {result['positive_rate']*100:.1f}%\n"
                    f"CV AUC {result.get('cv_auc')}"
                )
            else:
                logger.info("메타 모델 학습 보류: %s", result.get("reason"))
        except Exception as e:
            logger.exception("데이터 유지보수/학습 실패: %s", e)

    def _load_meta_model(self):
        """파일이 갱신됐을 때만 다시 읽는다."""
        try:
            mtime = os.path.getmtime(self.meta_model_path)
        except OSError:
            return None
        if self._meta_bundle is None or mtime > self._meta_mtime:
            bundle = MetaTrainer.load_model(self.meta_model_path)
            if bundle:
                self._meta_bundle = bundle
                self._meta_mtime = mtime
                logger.info("메타 모델 로드: %s", bundle.get("meta", {}))
        return self._meta_bundle

    def _meta_probability(self, obs: Optional[np.ndarray], action: str,
                          confidence: float, crypto_score: float,
                          news_score: float) -> Optional[float]:
        """
        학습된 메타 모델이 있을 때만 성공 확률을 반환. 없으면 None.
        입력 구성은 meta_trainer.build_dataset 과 동일해야 한다.
        """
        if obs is None:
            return None
        bundle = self._load_meta_model()
        if not bundle:
            return None

        try:
            side = 1.0 if action == "BUY" else -1.0
            row = list(map(float, np.asarray(obs).ravel())) + [
                float(confidence), float(crypto_score), float(news_score), side,
            ]
            names = bundle["meta"]["feature_names"]
            if len(row) != len(names):
                logger.warning(
                    "메타 모델 입력 차원 불일치 (%d != %d) - 필터 미적용",
                    len(row), len(names),
                )
                return None
            import pandas as pd
            X = pd.DataFrame([row], columns=names)
            return float(bundle["model"].predict_proba(X)[:, 1][0])
        except Exception as e:
            logger.warning("메타 모델 예측 실패: %s", e)
            return None

    # ------------------------------------------------------------------
    async def start(self):
        mode = "DRY-RUN" if Config.DRY_RUN else "LIVE"
        logger.info("AI 퀀트 트레이딩 봇 시작 (%s)", mode)

        await self.engine.reconcile_on_startup()
        self.rl_agent.load_model()

        await self.engine.notifier.notify_startup(
            mode,
            f"종목 {', '.join(Config.TARGET_TICKERS)} | {self.engine.risk.summary()}",
        )

        self.engine.clock.add_iterator(self.process_tick)
        self._install_signal_handlers()

        runner = asyncio.create_task(self.engine.run(), name="engine")
        stopper = asyncio.create_task(self._shutdown.wait(), name="shutdown")

        done, pending = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)

        if stopper in done:
            logger.info("종료 신호 수신 - 정리 중...")
            self.engine.stop()
            try:
                await asyncio.wait_for(runner, timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("엔진 종료 타임아웃 - 강제 취소.")
                runner.cancel()
        else:
            stopper.cancel()

        await self._cleanup()

    def _install_signal_handlers(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except NotImplementedError:
                # Windows 는 add_signal_handler 미지원 -> KeyboardInterrupt 로 처리
                pass

    async def _cleanup(self):
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        # 버퍼에 남은 가격 데이터를 잃지 않도록 먼저 flush
        try:
            self.recorder.close()
        except Exception as e:
            logger.error("데이터 recorder 종료 실패: %s", e)
        try:
            self.cb.save_baseline()
        except Exception as e:
            logger.error("서킷브레이커 기준선 저장 실패: %s", e)
        self.rl_agent.save_model()
        try:
            await self.news_feed.close()
        except Exception as e:
            logger.error("뉴스 피드 세션 종료 실패: %s", e)
        try:
            await self.engine.notifier.notify_shutdown()
        except Exception:
            pass
        logger.info("종료 완료.")


def run():
    pipeline = MainPipeline()
    try:
        asyncio.run(pipeline.start())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt - 종료합니다.")


if __name__ == "__main__":
    run()
