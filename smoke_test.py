"""
오프라인 통합 점검 스크립트.

네트워크 호출 없이(업비트/텔레그램/Gemini 전부 미접속) 파이프라인 전 구간을
합성 웹소켓 데이터로 돌려, 검증에서 발견된 버그들이 실제로 잡혔는지 확인한다.

    python smoke_test.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

# 실주문 방지. Config 임포트 전에 반드시 설정해야 한다.
os.environ["DRY_RUN"] = "true"
os.environ.setdefault("UPBIT_OPEN_API_ACCESS_KEY", "smoke-test")
os.environ.setdefault("UPBIT_OPEN_API_SECRET_KEY", "smoke-test")

import numpy as np

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
def make_orderbook(code="KRW-BTC", base=100_000_000.0, tick=1000.0, holes=()):
    """정상/단절 호가창 메시지 생성. holes 에 지정한 단계에 큰 갭을 만든다."""
    units, ask, bid = [], base + tick, base - tick
    for i in range(15):
        gap = tick * (40 if i in holes else 1)
        ask += gap
        bid -= gap
        units.append({
            "ask_price": ask, "bid_price": bid,
            "ask_size": 0.5 + i * 0.01, "bid_size": 0.5 + i * 0.01,
        })
    return {
        "type": "orderbook", "code": code,
        "total_ask_size": sum(u["ask_size"] for u in units),
        "total_bid_size": sum(u["bid_size"] for u in units),
        "orderbook_units": units,
    }


def feed_market(market, code="KRW-BTC", n=60, holes=()):
    price = 100_000_000.0
    rng = np.random.default_rng(0)
    for i in range(n):
        price *= 1.0 + rng.normal(0, 0.0004)
        market.on_message({"type": "ticker", "code": code, "trade_price": price})
        market.on_message(make_orderbook(code, base=price, holes=holes))
        market.on_message({
            "type": "trade", "code": code, "trade_price": price,
            "trade_volume": 0.01, "ask_bid": "BID" if i % 2 else "ASK",
        })
    return price


# ---------------------------------------------------------------------------
def test_market_state():
    print("\n[1] 웹소켓 -> 시장상태 배선 (기존: add_callback 미호출로 데이터 전량 폐기)")
    from market_state import MarketState

    m = MarketState(["KRW-BTC"])
    check("콜백 이전 미준비", not m.is_ready("KRW-BTC"))

    feed_market(m)
    st = m.get("KRW-BTC")

    check("메시지 수신 집계", m.message_count == 180, f"{m.message_count}건")
    check("시장 데이터 준비 완료", m.is_ready("KRW-BTC"))
    check("호가 사다리가 실제 가격", len(st.depth_curve()) == 30,
          f"{len(st.depth_curve())}단계")

    obs = st.feature_vector(20)
    check("관측벡터 20차원", obs.shape == (20,))
    check("관측벡터가 난수 아님(유한값)", bool(np.all(np.isfinite(obs))))
    check("관측벡터가 전부 0이 아님", float(np.abs(obs).sum()) > 0)

    z = st.viscosity()
    check("점성 z_t 가 [0,1] 범위", 0.0 <= z <= 1.0, f"z={z:.4f}")
    check("점성이 결정적(같은 입력=같은 값)", z == st.viscosity())
    return m


def test_circuit_breaker():
    print("\n[2] 서킷 브레이커 (기존: AND 결합으로 5만틱 발동 0회)")
    from market_state import MarketState
    from circuit_breaker import CircuitBreaker

    cb = CircuitBreaker()

    normal = MarketState(["KRW-BTC"])
    feed_market(normal)
    normal_depth = normal.get("KRW-BTC").depth_curve()

    broken = MarketState(["KRW-BTC"])
    feed_market(broken, holes=(3, 7, 11))
    broken_depth = broken.get("KRW-BTC").depth_curve()

    # 기동 직후(기준선 없음)에는 판정하지 않아야 한다
    early = cb.evaluate(0.1, 0.5, broken_depth, "KRW-BTC")
    check("워밍업 전에는 호가 판정 보류", not early.triggered, early.describe())
    check("워밍업 전 임계값 없음", cb.betti_threshold("KRW-BTC") is None)

    # 정상 호가창으로 기준선 학습
    for _ in range(cb.warmup):
        cb.evaluate(0.1, 0.5, normal_depth, "KRW-BTC")
    thr = cb.betti_threshold("KRW-BTC")
    check("기준선 학습 완료", thr is not None, f"임계 Betti-0={thr:.0f}")

    calm = cb.evaluate(0.1, 0.5, normal_depth, "KRW-BTC")
    check("정상 시장에서 미발동", not calm.triggered, calm.describe())

    b0n = cb.compute_betti_0(normal_depth)
    b0b = cb.compute_betti_0(broken_depth)
    check("단절 호가창 Betti-0 상승", b0b > b0n, f"정상={b0n} / 단절={b0b}")

    # 순간 스파이크는 무시하고 지속될 때만 발동해야 한다
    # (BTC 실측 단일틱 오발동률 0.10% = 1초틱 기준 약 17분마다 1회)
    shred = np.array([100.0, 101, 102, 500, 501, 900, 901, 1400, 1401,
                      2000, 2001, 2700, 2701, 3500, 3501, 4400])
    spike = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("단절 1틱은 무시(스파이크 필터)", not spike.triggered, spike.describe())
    for _ in range(cb.betti_persist - 2):
        cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    sustained = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("단절 지속 시 발동", sustained.triggered, sustained.describe())

    # 정상 호가가 한 번 들어오면 연속 카운터가 끊겨야 한다
    cb.evaluate(0.1, 0.5, normal_depth, "KRW-BTC")
    reset = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("정상 복귀 시 연속 카운터 초기화", not reset.triggered, reset.describe())

    fired = cb.evaluate(0.95, 0.5, normal_depth, "KRW-BTC")
    check("점성 단독으로 발동(OR 결합)", fired.triggered, fired.describe())

    fired2 = cb.evaluate(0.1, -0.8, normal_depth, "KRW-BTC")
    check("곡률 붕괴 단독으로 발동", fired2.triggered, fired2.describe())

    # 종목마다 정상 Betti-0 가 달라도 각자 기준선으로 판정되는지
    cb2 = CircuitBreaker()
    for _ in range(cb2.warmup + 5):
        cb2.evaluate(0.1, 0.5, shred, "KRW-WEIRD")
    weird = cb2.evaluate(0.1, 0.5, shred, "KRW-WEIRD")
    check("구조가 다른 종목은 자기 기준선 적용", not weird.triggered,
          f"단절이 평상시인 종목 -> 미발동 (임계 {cb2.betti_threshold('KRW-WEIRD'):.0f})")


def test_curvature():
    print("\n[3] 곡률 지표 (기존: mean(eig)-max(eig) 로 항상 음수)")
    from fisher_geometry import FisherGeometry

    fg = FisherGeometry()
    healthy = fg.compute_fisher_information(np.random.default_rng(1).standard_normal((100, 5)))
    rank1 = np.outer(np.random.default_rng(2).standard_normal(100),
                     np.random.default_rng(3).standard_normal(5))
    degenerate = fg.compute_fisher_information(rank1)

    k_ok = fg.get_ricci_scalar_curvature(healthy)
    k_bad = fg.get_ricci_scalar_curvature(degenerate)
    check("정상 매니폴드는 양수 곡률", k_ok > 0, f"kappa={k_ok:.4f}")
    check("퇴화 매니폴드는 음수 곡률", k_bad < 0, f"kappa={k_bad:.4f}")
    check("부호가 실제로 갈림", k_ok > k_bad)


def test_order_params():
    print("\n[4] 주문 파라미터 (기존: volume='' / price='' 를 그대로 전송)")
    import asyncio as aio
    import httpx, json
    from upbit import AsyncUpbit

    captured = {}

    def handler(request: httpx.Request):
        captured.clear()
        captured.update(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"uuid": "smoke-uuid", "state": "wait"})

    async def run():
        from upbit_client import UpbitClientWrapper
        w = UpbitClientWrapper.__new__(UpbitClientWrapper)
        from upbit_client import RateLimiter
        w.client = AsyncUpbit(access_key="a", secret_key="b",
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        w.order_limiter = RateLimiter(6, 180)
        w.query_limiter = RateLimiter(20, 800)

        await w.place_order(market="KRW-BTC", side="bid", ord_type="price", price="10000")
        buy = dict(captured)
        await w.place_order(market="KRW-BTC", side="ask", ord_type="market", volume="0.001")
        sell = dict(captured)
        return buy, sell

    buy, sell = aio.run(run())
    check("시장가 매수에 volume 미포함", "volume" not in buy, str(buy))
    check("시장가 매수에 price 포함", buy.get("price") == "10000")
    check("시장가 매도에 price 미포함", "price" not in sell, str(sell))
    check("시장가 매도에 volume 포함", sell.get("volume") == "0.001")


def test_order_result_parsing():
    print("\n[5] 주문 결과 판정 (기존: 'uuid' in res 가 항상 False)")
    from upbit.types.order import Order

    res = Order.construct(uuid="abc-123", state="wait")
    check("구버전 판정은 실패했음", ("uuid" in res) is False, "pydantic __iter__ 가 튜플을 냄")
    check("속성 접근은 정상", res.uuid == "abc-123")


def test_risk_manager():
    print("\n[6] 리스크 관리 (기존: 계층 자체가 없었음)")
    from config import Config
    from risk_manager import RiskManager

    tmp = tempfile.mkdtemp()
    try:
        rm = RiskManager(state_dir=tmp)

        d = rm.check_buy("KRW-BTC", 1_000, 0, 0, 1_000_000)
        check("최소 주문금액 미달 거부", not d, d.reason)

        d = rm.check_buy("KRW-BTC", 10_000, 0, 0, 5_000)
        check("잔고 부족 거부", not d, d.reason)

        d = rm.check_buy("KRW-BTC", 10_000, Config.MAX_POSITION_KRW, 0, 1_000_000)
        check("종목 한도 초과 거부", not d, d.reason)

        d = rm.check_buy("KRW-BTC", 10_000, 0, Config.MAX_TOTAL_EXPOSURE_KRW, 1_000_000)
        check("전체 노출 한도 초과 거부", not d, d.reason)

        d = rm.check_sell("KRW-BTC", 0.001, 0.0, 100_000_000)
        check("미보유 매도 거부", not d, d.reason)

        d = rm.check_sell("KRW-BTC", 1.0, 0.001, 100_000_000)
        check("보유수량 초과 매도 거부", not d, d.reason)

        d = rm.check_buy("KRW-BTC", 10_000, 0, 0, 1_000_000)
        check("정상 매수는 통과", bool(d), d.reason or "허용")

        rm.register_order("KRW-BTC")
        d = rm.check_buy("KRW-BTC", 10_000, 0, 0, 1_000_000)
        check("쿨다운 중 재주문 거부", not d, d.reason)

        # 일일 손실 한도
        rm2 = RiskManager(state_dir=tempfile.mkdtemp())
        rm2.update_equity(1_000_000)
        pnl = rm2.update_equity(1_000_000 - Config.DAILY_LOSS_LIMIT_KRW - 1)
        check("일일 손실 한도 도달 시 정지", rm2.halted, f"pnl={pnl:,.0f} / {rm2.halt_reason}")
        d = rm2.check_buy("KRW-ETH", 10_000, 0, 0, 1_000_000)
        check("정지 상태에서 신규 매수 차단", not d, d.reason)

        # 영속화 (EC2 재시작 시 한도 리셋 방지)
        rm2._save()
        rm3 = RiskManager(state_dir=rm2.state_dir)
        check("재시작 후에도 정지 상태 유지", rm3.halted, "디스크에서 복원됨")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scheduler():
    print("\n[7] 스케줄러 (기존: int(time.time()) % N == 0 로 주기 누락)")
    from main import Scheduler

    s = Scheduler()
    check("첫 호출은 미발동(한 주기 대기)", not s.due("x", 0.05))
    import time as _t
    _t.sleep(0.06)
    check("주기 경과 후 발동", s.due("x", 0.05))
    check("연속 호출은 미발동", not s.due("x", 0.05))
    # 오래 밀려도 반드시 1회 발동하는지
    _t.sleep(0.2)
    check("크게 밀려도 발동 보장", s.due("x", 0.05))


def test_dry_run_execution():
    print("\n[8] DRY-RUN 주문 경로 (실제 API 미호출)")
    from config import Config
    from execution_engine import ExecutionEngine

    check("DRY_RUN 기본값이 안전측", Config.DRY_RUN is True)

    async def run():
        eng = ExecutionEngine.__new__(ExecutionEngine)
        from market_state import MarketState
        from risk_manager import RiskManager
        from notifier import TelegramNotifier

        eng.config = Config
        eng.market = MarketState(Config.TARGET_TICKERS)
        eng.risk = RiskManager(state_dir=tempfile.mkdtemp())
        eng.notifier = TelegramNotifier()
        eng.notifier.enabled = False
        eng.active_orders = {}
        eng.balances = {"KRW": {"balance": 1_000_000.0, "locked": 0.0, "avg_buy_price": 0.0}}
        eng._balances_ts = 9e18          # 갱신 시도 차단(네트워크 미사용)
        eng.sim_krw = 1_000_000.0
        eng.sim_positions = {}
        eng.client = None

        feed_market(eng.market)

        ok = await eng.place_market_buy("KRW-BTC", 10_000)
        check("DRY-RUN 매수 성공", ok)
        check("모의 KRW 차감", eng.sim_krw == 990_000.0, f"{eng.sim_krw:,.0f}")
        check("모의 포지션 증가", eng.sim_positions.get("BTC", 0) > 0)
        check("실주문 흔적 없음", len(eng.active_orders) == 0)

        ok2 = await eng.place_market_buy("KRW-BTC", 10_000)
        check("쿨다운으로 연속 매수 차단", not ok2)

        # 스프레드 과다 시 매수 차단
        st = eng.market.get("KRW-ETH")
        eng.market.on_message({"type": "ticker", "code": "KRW-ETH", "trade_price": 5_000_000.0})
        eng.market.on_message({
            "type": "orderbook", "code": "KRW-ETH",
            "total_ask_size": 1.0, "total_bid_size": 1.0,
            "orderbook_units": [{"ask_price": 5_100_000.0, "bid_price": 4_900_000.0,
                                 "ask_size": 1.0, "bid_size": 1.0}],
        })
        ok3 = await eng.place_market_buy("KRW-ETH", 10_000)
        check("스프레드 과다 시 매수 차단", not ok3, "슬리피지 한도 적용")

        await eng.notifier.close()

    asyncio.run(run())


def test_pbo_dsr():
    print("\n[9] 백테스트 지표 (기존: PBO 순위 계산 오류 / DSR 보정 누락)")
    from cross_validation import BacktestMetrics as BM

    rng = np.random.default_rng(11)
    alpha = np.vstack([rng.standard_normal(800) + 0.35, rng.standard_normal((49, 800))])
    pbo_alpha = BM.calculate_pbo(alpha, mc_sims=200, rng=rng)
    check("진짜 알파는 PBO 낮음", pbo_alpha < 0.2, f"PBO={pbo_alpha:.2f}")

    noise_pbos = [BM.calculate_pbo(rng.standard_normal((50, 800)), mc_sims=200, rng=rng)
                  for _ in range(9)]
    med = float(np.median(noise_pbos))
    check("노이즈는 PBO 높음", med > 0.35, f"PBO 중앙값={med:.2f}")

    T, n_trials = 1000, 200
    srs = []
    for _ in range(n_trials):
        x = rng.normal(0.0, 0.01, T)
        srs.append(x.mean() / x.std(ddof=1))
    var_trials = float(np.var(srs, ddof=1))

    dsr_good = BM.calculate_dsr(rng.normal(0.0012, 0.01, T), n_trials, var_trials)
    dsr_bad = BM.calculate_dsr(rng.normal(0.0, 0.01, T), n_trials, var_trials)
    check("실력 있는 전략 DSR 높음", dsr_good > 0.5, f"DSR={dsr_good:.3f}")
    check("운 좋은 전략 DSR 낮음", dsr_bad < 0.2, f"DSR={dsr_bad:.3f}")


def test_replay_and_rl():
    print("\n[10] RL 리플레이 버퍼 (기존: 난수로 학습)")
    from free_energy_ppo import MarketReplayBuffer, FreeEnergyEnv

    buf = MarketReplayBuffer()
    env = FreeEnergyEnv(buf)
    obs, _, _, truncated, info = env.step(np.zeros(3, dtype=np.float32))
    check("표본 없으면 보상 0 + 조기종료", truncated and info.get("starved") is True)

    rng = np.random.default_rng(5)
    for _ in range(200):
        buf.push(rng.standard_normal(20).astype(np.float32), 0.01)
    _, reward, _, _, info2 = env.step(np.ones(3, dtype=np.float32))
    check("롱 포지션 + 상승 = 양수 보상", reward > 0, f"reward={reward:.5f}")
    _, reward2, _, _, _ = env.step(-np.ones(3, dtype=np.float32))
    check("숏 포지션 + 상승 = 음수 보상", reward2 < 0, f"reward={reward2:.5f}")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)   # 점검 결과만 보이도록 로그 억제

    print("=" * 70)
    print("코인봇 오프라인 통합 점검 (네트워크 호출 없음)")
    print("=" * 70)

    test_market_state()
    test_circuit_breaker()
    test_curvature()
    test_order_params()
    test_order_result_parsing()
    test_risk_manager()
    test_scheduler()
    test_dry_run_execution()
    test_pbo_dsr()
    test_replay_and_rl()

    print("\n" + "=" * 70)
    print(f"결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("실패 항목:")
        for f in FAIL:
            print("  -", f)
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
