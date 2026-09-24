"""
오프라인 통합 점검 스크립트.

네트워크 호출 없이(업비트/텔레그램/Gemini 전부 미접속) 파이프라인 전 구간을
합성 웹소켓 데이터로 돌려, 검증에서 발견된 버그들이 실제로 잡혔는지 확인한다.

    python smoke_test.py
"""

import asyncio
import glob
import json
import os
import shutil
import sys
import tempfile
import time

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


def make_shred(n_breaks: int, base=100.0, step=1.0, jump=500.0):
    """
    단절이 n_breaks 개 있는 호가 사다리를 만든다.

    compute_betti_0 은 임계값을 '중앙값 갭의 gap_multiple 배'로 잡으므로,
    단절이 과반이 되면 중앙값 자체가 커져 아무 것도 안 걸린다. 정상 갭을
    단절 1개당 2개씩 끼워 중앙값이 정상 갭 쪽에 남도록 한다.

    픽스처를 임계값 경계에 딱 붙여두면 margin 을 조정할 때마다 테스트가
    깨지므로, 호출부는 실측 노이즈 최대치(BTC 기준 18)를 확실히 넘는
    수준을 쓴다.
    """
    xs, x = [base], base
    for _ in range(n_breaks):
        for _ in range(2):
            x += step
            xs.append(x)
        x += jump
        xs.append(x)
    return np.asarray(xs)


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

    # 위상 조건은 2026-09-18 보정으로 기본 비활성이다(지표가 유동성 증발을
    # 감지하지 못함 - 아래 [2d] 참고). 지속/리셋 메커니즘 자체는 여전히
    # 살아 있어야 하므로 명시적으로 켠 인스턴스로 검증한다.
    cb = CircuitBreaker(topology_enabled=True)

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

    # 순간 스파이크는 무시하고 지속될 때만 발동해야 한다.
    # 실측 노이즈 최대치(BTC 18)를 확실히 넘는 '진짜 단절' 수준을 쓴다 -
    # 경계선 픽스처는 margin 을 조정할 때마다 깨진다.
    shred = make_shred(20)
    check("합성 단절이 판정 임계값을 넘는 수준",
          cb.compute_betti_0(shred) > thr,
          f"betti0={cb.compute_betti_0(shred)} > 임계 {thr:.0f}")
    spike = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("단절 1틱은 무시(스파이크 필터)", not spike.triggered, spike.describe())
    for _ in range(cb.betti_persist - 2):
        cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    sustained = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("단절 지속 시 발동", sustained.triggered, sustained.describe())

    # 발동 직후에도 카운터가 0 으로 리셋돼야 한다. 리셋이 없으면 단절이
    # 지속되는 동안 매 틱 재발동 판정이 서서, main.py 쿨다운이 풀릴 때마다
    # 같은 사건으로 알림이 반복된다(새벽 42회 연속 발동의 직접 원인).
    again = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("발동 직후 연속 카운터 리셋", not again.triggered, again.describe())
    for _ in range(cb.betti_persist - 2):
        cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("리셋 후 persist 틱을 다시 채워야 재발동",
          cb.evaluate(0.1, 0.5, shred, "KRW-BTC").triggered)

    # 정상 호가가 한 번 들어오면 연속 카운터가 끊겨야 한다
    cb.evaluate(0.1, 0.5, normal_depth, "KRW-BTC")
    reset = cb.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("정상 복귀 시 연속 카운터 초기화", not reset.triggered, reset.describe())

    # 기본값에서는 같은 단절이 발동하지 않아야 한다
    cb_off = CircuitBreaker()
    for _ in range(cb_off.warmup):
        cb_off.evaluate(0.1, 0.5, normal_depth, "KRW-BTC")
    for _ in range(cb_off.betti_persist + 2):
        last_off = cb_off.evaluate(0.1, 0.5, shred, "KRW-BTC")
    check("위상 조건 기본 비활성", not last_off.triggered, last_off.describe())

    fired = cb.evaluate(0.95, 0.5, normal_depth, "KRW-BTC")
    check("점성 단독으로 발동(OR 결합)", fired.triggered, fired.describe())

    # 곡률은 2026-09-18 보정으로 기본 비활성 (한산함을 위험으로 오독하던 문제).
    # 끈 상태에서 안 걸리고, 명시적으로 켰을 때만 걸리는지 둘 다 확인한다.
    off = cb.evaluate(0.1, -0.8, normal_depth, "KRW-BTC")
    check("곡률 조건 기본 비활성", not off.triggered, off.describe())
    cb_k = CircuitBreaker(curvature_enabled=True)
    on = cb_k.evaluate(0.1, -0.8, normal_depth, "KRW-BTC")
    check("곡률 명시적으로 켜면 단독 발동", on.triggered, on.describe())

    # 종목마다 정상 Betti-0 가 달라도 각자 기준선으로 판정되는지
    cb2 = CircuitBreaker()
    for _ in range(cb2.warmup + 5):
        cb2.evaluate(0.1, 0.5, shred, "KRW-WEIRD")
    weird = cb2.evaluate(0.1, 0.5, shred, "KRW-WEIRD")
    check("구조가 다른 종목은 자기 기준선 적용", not weird.triggered,
          f"단절이 평상시인 종목 -> 미발동 (임계 {cb2.betti_threshold('KRW-WEIRD'):.0f})")


def test_betti_cannot_detect_withdrawal():
    """
    Betti-0 가 왜 꺼져 있는지를 코드로 고정해 둔다.

    유동성 위기 = 호가가 취소되어 단계가 사라지는 것인데, 이 지표는 그
    상황에서 값이 '내려간다'. 나중에 지표를 재설계하면 이 테스트가 깨지고,
    그때 topology_enabled 기본값을 되돌릴지 함께 판단하면 된다.
    """
    print("\n[2d] 위상 지표 한계 (이 조건이 기본 비활성인 이유)")
    from market_state import MarketState
    from circuit_breaker import CircuitBreaker

    m = MarketState(["KRW-BTC"])
    feed_market(m)
    depth = m.get("KRW-BTC").depth_curve()
    cb = CircuitBreaker()

    rng = np.random.default_rng(0)
    vals = {}
    for frac in (0.0, 0.6, 0.95):
        keep = max(3, int(round(len(depth) * (1 - frac))))
        idx = np.sort(rng.choice(len(depth), size=keep, replace=False))
        vals[frac] = cb.compute_betti_0(depth[idx])

    check("호가 60% 증발 시 Betti-0 가 오르지 않음(역전)",
          vals[0.6] <= vals[0.0],
          f"정상={vals[0.0]} -> 60%증발={vals[0.6]}")
    check("호가 95% 증발이 '정상'으로 판정됨",
          vals[0.95] <= 3,
          f"95%증발 Betti-0={vals[0.95]} (정상 호가창과 구분 불가)")
    check("그래서 기본 비활성", not CircuitBreaker().topology_enabled)


def test_recorder_replayability():
    print("\n[2c] 기록기 재생 가능성 (기존: 브레이커 입력을 안 남겨 사후 분석 불가)")
    import csv as _csv
    from market_state import MarketState
    from circuit_breaker import CircuitBreaker
    from data_recorder import DataRecorder

    m = MarketState(["KRW-BTC"])
    feed_market(m, holes=(3, 7, 11))
    st = m.get("KRW-BTC")
    cb = CircuitBreaker()

    tmp = tempfile.mkdtemp(prefix="coinbot-rec-")
    try:
        rec = DataRecorder(tmp, flush_interval=0.0, flush_rows=1)
        result = cb.evaluate(0.1, 0.5, st.depth_curve(), "KRW-BTC")
        rec.record_prices(m, ["KRW-BTC"], diag={"KRW-BTC": {
            "betti0": result.betti_0, "cb_thr": "", "kappa": 0.4242,
        }})
        rec.flush()

        with open(os.path.join(tmp, "prices", rec._today() + ".csv"),
                  encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        check("가격 CSV 에 브레이커 진단 컬럼 존재",
              {"betti0", "cb_thr", "kappa"} <= set(rows[0].keys()),
              ",".join(rows[0].keys()))
        check("기록된 betti0 이 판정값과 일치",
              int(rows[0]["betti0"]) == result.betti_0,
              f"기록={rows[0]['betti0']} / 판정={result.betti_0}")
        check("기록된 kappa 가 판정 시점 값", float(rows[0]["kappa"]) == 0.4242)

        # 호가 사다리 원본 -- 저속 기록 + 재생 가능성
        check("호가 사다리 스냅샷 기록", rec.record_depth(m, ["KRW-BTC"]))
        check("간격 미도달 시 재기록 안 함",
              not rec.record_depth(m, ["KRW-BTC"]))

        with open(os.path.join(tmp, "depth", rec._today() + ".jsonl"),
                  encoding="utf-8") as f:
            snap = json.loads(f.readline())

        # 이게 2단계의 핵심: 저장본만으로 Betti-0 를 다시 계산했을 때
        # 라이브 판정과 같은 값이 나와야 사후 재보정이 성립한다.
        ladder = np.asarray(list(reversed(snap["bids"])) + snap["asks"], dtype=float)
        check("저장본으로 depth_curve 복원", np.allclose(ladder, st.depth_curve()),
              f"{len(ladder)}단계")
        check("저장본만으로 Betti-0 재계산 일치",
              cb.compute_betti_0(ladder) == result.betti_0,
              f"재생={cb.compute_betti_0(ladder)} / 원본={result.betti_0}")

        stats = rec.stats()
        check("stats 에 호가 스냅샷 반영", stats["세션_호가스냅샷"] == 1, str(stats))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 운영 중 컬럼 추가 시 기존 파일이 깨지지 않아야 한다 ----
    # 실제로 betti0/cb_thr/kappa 를 추가한 날, 그날 파일이 '9컬럼 헤더 +
    # 12필드 행' 으로 깨져 자정 Parquet 압축이 ParserError 로 실패했다.
    import csv as _csv2
    tmp2 = tempfile.mkdtemp(prefix="coinbot-schema-")
    try:
        rec2 = DataRecorder(tmp2)
        day_path = rec2._price_path(rec2._today())

        # 구버전 스키마로 파일이 이미 있는 상황을 만든다
        with open(day_path, "w", newline="", encoding="utf-8") as f:
            w = _csv2.writer(f)
            w.writerow(["ts", "ticker", "mid"])
            w.writerow([1.0, "KRW-BTC", 100.0])

        m2 = MarketState(["KRW-BTC"])
        feed_market(m2)
        rec2.record_prices(m2, ["KRW-BTC"], diag={"KRW-BTC": {"betti0": 7}})
        rec2.flush()

        with open(day_path, encoding="utf-8") as f:
            rows = list(_csv2.reader(f))
        widths = {len(r) for r in rows}
        check("스키마 바뀌면 새 헤더로 다시 시작",
              rows[0] == list(DataRecorder.PRICE_HEADER), str(rows[0]))
        check("한 파일 안에 필드 수가 섞이지 않음", len(widths) == 1, f"필드수={widths}")

        backups = [p for p in os.listdir(os.path.join(tmp2, "prices"))
                   if ".cols" in p]
        check("기존 파일은 보존됨(데이터 손실 없음)", len(backups) == 1, str(backups))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


def test_decide_action():
    print("\n[2f] 최종 판정 규칙 (기존: LLM 산문 판정 / 그 뒤 뉴스 거부권)")
    from multi_agent import decide_action, DEFAULT_MIN_SCORE, CRYPTO_WEIGHT, NEWS_WEIGHT
    from config import Config

    buy = decide_action(0.65, 0.20)
    sell = decide_action(-0.65, -0.20)
    check("가중합이 임계 이상이면 BUY", buy["action"] == "BUY", buy["reason"])
    check("가중합이 임계 이하(음)면 SELL", sell["action"] == "SELL", sell["reason"])

    # LLM 산문 판정이 HOLD 로 뭉갰던 실제 케이스
    revived = decide_action(0.85, 0.25)
    check("LLM 이 놓쳤던 케이스(0.85/0.25) -> BUY", revived["action"] == "BUY",
          revived["reason"])

    # 핵심 회귀: 부호 일치 규칙이 뉴스에 거부권을 줘서 놓쳤던 실제 급등 구간.
    # 2026-09-18 16:04 KST, BTC 1분봉 급등 중 crypto=+0.72 인데 news=-0.20
    # 때문에 HOLD 로 막혔다. 가중합에서는 0.7*0.72 + 0.3*(-0.20) = 0.444 로
    # 진입해야 한다.
    rally = decide_action(0.72, -0.20)
    check("약한 역방향 뉴스가 강한 크립토를 막지 못함(실제 급등 구간)",
          rally["action"] == "BUY", rally["reason"])

    # 그렇다고 뉴스가 무시되는 것도 아니다 - 문턱을 올리고 내린다
    check("호재 뉴스는 문턱을 낮춤 (crypto 0.40 단독은 미달, +뉴스면 진입)",
          decide_action(0.40, 0.0)["action"] == "HOLD"
          and decide_action(0.40, 0.45)["action"] == "BUY")
    check("강한 악재는 사실상 거부권처럼 작동 (crypto 0.72 라도 news -0.9 면 HOLD)",
          decide_action(0.72, -0.9)["action"] == "HOLD",
          decide_action(0.72, -0.9)["reason"])

    # 임계값 경계 (min_score 를 직접 넘겨 검증)
    check("임계값 미만이면 HOLD", decide_action(0.50, 0.0, 0.4)["action"] == "HOLD",
          f"0.7*0.50={0.35}")
    check("임계값 이상이면 진입", decide_action(0.58, 0.0, 0.4)["action"] == "BUY")

    # News Agent 실패(score 0.0) - 막지는 않되 crypto 기준이 올라간다
    check("뉴스 실패 시 crypto 단독 기준으로 올라감",
          decide_action(0.50, 0.0)["action"] == "HOLD"
          and decide_action(0.60, 0.0)["action"] == "BUY",
          "0.7c >= 0.4 -> c >= 0.571")

    # 강도는 메타 모델 입력이자 대체 게이트 기준이라 스케일이 맞아야 한다
    s = decide_action(0.60, 0.40)["strength"]
    check("강도 = |0.7*crypto + 0.3*news|",
          abs(s - abs(CRYPTO_WEIGHT * 0.6 + NEWS_WEIGHT * 0.4)) < 1e-9, f"강도={s}")
    check("진입 최소 강도가 대체 게이트 기준 이상(이중 차단 방지)",
          DEFAULT_MIN_SCORE >= Config.META_FALLBACK_MIN_STRENGTH,
          f"{DEFAULT_MIN_SCORE} >= {Config.META_FALLBACK_MIN_STRENGTH}")


def test_news_feed():
    print("\n[2e] 뉴스 피드 (기존: 고정 문자열 - news_score 항상 0)")
    import asyncio
    from news_feed import (
        parse_cryptopanic, parse_naver, parse_newsapi, parse_gdelt,
        merge_headlines, NewsFeed, _FALLBACK_TEXT, queries_for,
    )

    # ---- 파싱: 실제 API 응답 형태로(2026-09-18 실측) 검증. 네트워크 호출 없음 ----
    cp_raw = {"results": [{"title": "Bitcoin surges past key level"},
                          {"title": "  "}, {"title": "Ethereum ETF inflow record"}]}
    check("CryptoPanic 파싱 - 제목 추출, 빈 제목 제외",
          parse_cryptopanic(cp_raw) == ["Bitcoin surges past key level",
                                        "Ethereum ETF inflow record"])

    # 네이버는 제목에 <b> 태그와 HTML 엔티티를 섞어 준다 (실측 그대로)
    nv_raw = {"items": [
        {"title": "JP모건 &quot;ETF 헤지 줄어들면, <b>비트코인</b>이 금보다 더 오른다&quot;"},
        {"title": "<b>암호화폐</b> 거래소 점검 공지"},
    ]}
    nv_parsed = parse_naver(nv_raw)
    check("네이버 파싱 - HTML 태그/엔티티 제거",
          nv_parsed == ['JP모건 "ETF 헤지 줄어들면, 비트코인이 금보다 더 오른다"',
                        "암호화폐 거래소 점검 공지"],
          str(nv_parsed))

    na_ok = {"status": "ok", "articles": [{"title": "Why Bitcoin's Price Is Spiking"}]}
    na_err = {"status": "error", "message": "rate limited"}
    check("NewsAPI 파싱 - status=ok", parse_newsapi(na_ok) == ["Why Bitcoin's Price Is Spiking"])
    check("NewsAPI 파싱 - status=error 는 빈 목록(쿼터 초과 등)", parse_newsapi(na_err) == [])

    check("응답 형태가 아니면(None/에러 페이지) 빈 목록",
          parse_cryptopanic(None) == [] and parse_naver({"error": "x"}) == [])

    # GDELT 는 제목이 토큰 단위로 띄어져 온다 (" Shares Up 6 . 8 % ")
    gd = {"articles": [{"title": "Bitcoin  Rallies   Past  $70 , 000"}, {"title": "  "}]}
    check("GDELT 파싱 - 중복 공백 정리",
          parse_gdelt(gd) == ["Bitcoin Rallies Past $70 , 000"], str(parse_gdelt(gd)))
    check("GDELT 응답 아님 -> 빈 목록", parse_gdelt(None) == [] and parse_gdelt({}) == [])

    # ---- 종목별 검색어 ----
    # 공유 뉴스를 쓰던 때 헤드라인이 BTC 중심이라 알트 판정을 깎았다
    # (SOL 부호 불일치 76%, 신호 -25%). 종목마다 달라야 한다.
    ko = {t: queries_for(t)["ko"] for t in
          ("KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL")}
    check("종목별 한국어 검색어가 서로 다름", len(set(ko.values())) == 4, str(ko))
    check("BTC 검색어에 비트코인 포함", "비트코인" in ko["KRW-BTC"])
    check("SOL 검색어에 솔라나 포함", "솔라나" in ko["KRW-SOL"])
    check("모르는 종목은 일반 크립토 검색어로 폴백",
          queries_for("KRW-DOGE")["ko"] == queries_for(None)["ko"])

    # ---- 병합: 중복 제거, 전부 비면 기존과 동일한 중립 문구로 폴백 ----
    merged = merge_headlines(["A", "B"], ["B", "C"])
    check("헤드라인 병합 - 중복 제거, 순서 보존", merged.split("\n") == ["- A", "- B", "- C"], merged)
    check("전부 비면 중립 문구로 폴백(하위호환)", merge_headlines([], []) == _FALLBACK_TEXT)

    # ---- 캐시: TTL 안에서는 재조회하지 않는다 (무료 쿼터 보호가 목적) ----
    # 실제 소스 호출(_fetch_all)은 네트워크가 필요하므로 여기서는 대체해
    # 캐시 동작만 검증한다.
    async def _run_cache_test():
        nf = NewsFeed(ttl_sec=1000.0)
        calls = {"n": 0}

        async def fake_fetch_all(ticker=None):
            calls["n"] += 1
            return f"headline-{calls['n']}"

        nf._fetch_all = fake_fetch_all

        first = await nf.get_headlines("KRW-BTC")
        second = await nf.get_headlines("KRW-BTC")
        check("TTL 안에서는 캐시 재사용(쿼터 절약)", first == second == "headline-1",
              f"{first} / {second}")

        # 캐시는 종목별로 분리돼야 한다. 공유 캐시였다면 ETH 가 BTC 헤드라인을
        # 그대로 받아, 종목별 쿼리를 넣은 의미가 없어진다.
        eth = await nf.get_headlines("KRW-ETH")
        check("종목이 다르면 캐시를 공유하지 않음", eth == "headline-2", eth)
        check("BTC 캐시는 그대로 유지",
              await nf.get_headlines("KRW-BTC") == "headline-1")

        text, ts = nf._cache["KRW-BTC"]
        nf._cache["KRW-BTC"] = (text, ts - 2000.0)   # TTL 만료를 흉내
        third = await nf.get_headlines("KRW-BTC")
        check("TTL 만료 후 재조회", third == "headline-3", third)

        await nf.close()

    asyncio.run(_run_cache_test())


def test_circuit_breaker_persistence():
    print("\n[2b] 서킷브레이커 기준선 영속화 (기존: 재시작마다 초기화되어 오발동 유발)")
    from circuit_breaker import CircuitBreaker
    from market_state import MarketState

    normal = MarketState(["KRW-BTC"])
    feed_market(normal)
    depth = normal.get("KRW-BTC").depth_curve()

    tmp = tempfile.mkdtemp()
    try:
        state_path = os.path.join(tmp, "cb_state.json")

        cb1 = CircuitBreaker(state_path=state_path)
        check("저장 전 워밍업 전(임계값 없음)", cb1.betti_threshold("KRW-BTC") is None)
        for _ in range(cb1.warmup):
            cb1.evaluate(0.1, 0.5, depth, "KRW-BTC")
        thr_before = cb1.betti_threshold("KRW-BTC")
        check("워밍업 후 기준선 생김", thr_before is not None)
        cb1.save_baseline()

        # 재시작 시뮬레이션: 새 인스턴스가 저장된 기준선을 즉시 복원해야 한다
        cb2 = CircuitBreaker(state_path=state_path)
        thr_after = cb2.betti_threshold("KRW-BTC")
        check("재시작 직후 워밍업 없이 판정 가능", thr_after is not None,
              f"임계값={thr_after}")
        check("복원된 임계값이 저장 전과 동일", thr_after == thr_before,
              f"{thr_before} == {thr_after}")

        # 오래된 기준선은 폐기해야 한다 (며칠 전 시장 구조를 그대로 쓰면 위험)
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        data["saved_at"] = time.time() - 999999   # 아주 오래전
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        cb3 = CircuitBreaker(state_path=state_path, baseline_max_age_sec=3600.0)
        check("오래된 기준선은 폐기하고 워밍업부터 다시", cb3.betti_threshold("KRW-BTC") is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_labeling_barrier_scale():
    print("\n[9b] 삼중장벽 스케일 (기존: 관측당 변동성을 누적수익률과 비교)")
    from meta_trainer import MetaTrainer, ROUND_TRIP_COST, MIN_BARRIER_MULT

    mt = MetaTrainer(tempfile.mkdtemp(), "/tmp/unused.pkl")

    # 1초 간격, 알려진 변동성으로 합성 가격 생성
    rng = np.random.default_rng(0)
    n = 3600
    tick_sigma = 0.00005
    ts = np.arange(n, dtype=float)
    px = 100_000_000.0 * np.exp(np.cumsum(rng.normal(0, tick_sigma, n)))

    per_obs = mt._ewma_vol(px)
    horizon = mt._horizon_vol(ts, px)
    expected = per_obs * np.sqrt(mt.horizon_sec / 1.0)

    check("관측당 변동성은 설정값과 일치", abs(per_obs - tick_sigma) / tick_sigma < 0.1,
          f"{per_obs:.6f} vs {tick_sigma:.6f}")
    check("horizon 변동성 = 관측당 x sqrt(horizon/dt)",
          abs(horizon - expected) < 1e-12, f"{horizon:.6f} vs {expected:.6f}")
    check("환산 배율이 sqrt(1800)≈42배", 40 < horizon / per_obs < 45,
          f"{horizon / per_obs:.1f}배")

    # 샘플링 간격이 1초가 아니어도 맞아야 한다 (기록 데이터에 최대 11초 간격 존재)
    h5 = mt._horizon_vol(ts * 5.0, px)
    check("샘플링 간격이 달라지면 환산도 따라감(5초 간격)",
          abs(h5 - per_obs * np.sqrt(mt.horizon_sec / 5.0)) < 1e-12,
          f"{h5:.6f}")

    # 핵심: 장벽이 왕복 마찰비용을 넘어야 '성공' 라벨이 수익을 뜻한다
    side, entry_i = 1, n - 1
    ts2 = np.arange(n + int(mt.horizon_sec) + 10, dtype=float)
    px2 = np.concatenate([px, px[-1] * np.ones(int(mt.horizon_sec) + 10)])
    res = mt.label_signal(float(ts2[entry_i]), side, ts2, px2)
    check("수직 장벽 도달 후 라벨 확정됨", res is not None)

    barrier = max(mt.pt_mult * horizon, MIN_BARRIER_MULT * ROUND_TRIP_COST)
    check("장벽이 왕복 마찰비용보다 큼(성공=수익)",
          barrier > ROUND_TRIP_COST,
          f"장벽 {barrier*100:.4f}% > 마찰 {ROUND_TRIP_COST*100:.4f}%")
    check("수정 전 방식이었다면 마찰비용 미만이었음(회귀 고정)",
          mt.pt_mult * per_obs < ROUND_TRIP_COST,
          f"구방식 {mt.pt_mult*per_obs*100:.4f}% < {ROUND_TRIP_COST*100:.4f}%")

    # 초저변동 구간에서도 하한이 걸려야 한다
    flat = np.full(n, 100_000_000.0)
    flat[1::2] += 1.0            # 거의 움직이지 않는 가격
    tiny = mt._horizon_vol(ts, flat)
    floored = max(mt.pt_mult * tiny, MIN_BARRIER_MULT * ROUND_TRIP_COST)
    check("초저변동 시 장벽 하한 적용",
          floored >= MIN_BARRIER_MULT * ROUND_TRIP_COST,
          f"{floored*100:.4f}%")


def test_shadow_mode():
    print("\n[5c] 섀도 모드 (BTC 만 실매매, 나머지는 판정·기록만)")
    import types
    from config import Config
    from main import MainPipeline
    from market_state import MarketState
    from data_recorder import DataRecorder

    tmp = tempfile.mkdtemp(prefix="coinbot-shadow-")
    try:
        # MainPipeline 은 생성자에서 실제 엔진/웹소켓을 만들므로 우회한다.
        pl = MainPipeline.__new__(MainPipeline)
        pl.primary_ticker = Config.TARGET_TICKERS[0]
        pl.recorder = DataRecorder(tmp)
        pl.replay = types.SimpleNamespace(ready=lambda n: False)
        pl.engine = types.SimpleNamespace(market=MarketState(Config.TARGET_TICKERS))

        orders = []

        async def fake_buy(ticker, amount=None):
            orders.append(("buy", ticker))
            return True

        async def fake_sell(ticker, volume=None):
            orders.append(("sell", ticker))
            return True

        pl.engine.place_market_buy = fake_buy
        pl.engine.place_market_sell = fake_sell
        pl._meta_probability = lambda *a, **k: None
        # 이 점검은 LLM 경로의 주문을 본다. 기본 전략은 가격행동이라
        # 그 모드에서는 LLM 판정이 주문을 내지 않는다(아래 별도 확인).
        saved_mode = Config.STRATEGY_MODE
        Config.STRATEGY_MODE = "llm"

        for t in Config.TARGET_TICKERS:
            feed_market(pl.engine.market, code=t)

        async def run():
            live, shadow = Config.TARGET_TICKERS[0], Config.TARGET_TICKERS[1]
            for ticker, is_live in ((live, True), (shadow, False)):
                st = pl.engine.market.get(ticker)
                # 진입이 확실히 서는 점수 (가중합 0.7*0.9+0.3*0.5 = 0.78)
                await pl._decide_one(ticker, st, st.mid_price or 1.0,
                                    0.9, 0.5, is_live)

        asyncio.run(run())

        path = os.path.join(tmp, "signals", pl.recorder._today() + ".jsonl")
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]

        live_t, shadow_t = Config.TARGET_TICKERS[0], Config.TARGET_TICKERS[1]
        by_ticker = {r["ticker"]: r for r in rows}

        check("실매매 종목은 주문이 나감", orders == [("buy", live_t)], str(orders))
        check("섀도 종목은 주문이 나가지 않음",
              all(t != shadow_t for _, t in orders), str(orders))
        check("두 종목 모두 신호로 기록됨",
              {live_t, shadow_t} <= set(by_ticker), str(list(by_ticker)))
        check("섀도 신호는 BUY/SELL 로 기록(메타 학습 표본이 됨)",
              by_ticker[shadow_t]["action"] == "BUY", str(by_ticker[shadow_t]))
        check("섀도 신호는 executed=False",
              by_ticker[shadow_t]["executed"] is False)
        check("섀도 사유가 구분되게 남음",
              "섀도" in by_ticker[shadow_t]["reason"], by_ticker[shadow_t]["reason"])

        # 가격행동 모드: LLM 판정은 기록만, 주문 없음 (두 경로 포지션 충돌 방지)
        Config.STRATEGY_MODE = "naked"
        orders.clear()

        async def run_naked():
            st = pl.engine.market.get(live_t)
            await pl._decide_one(live_t, st, st.mid_price or 1.0, 0.9, 0.5, True)

        asyncio.run(run_naked())
        with open(path, encoding="utf-8") as f:
            last = [json.loads(line) for line in f][-1]
        check("가격행동 모드에서 LLM 판정은 주문 안 냄", orders == [], str(orders))
        check("가격행동 모드에서도 LLM 판정은 기록됨",
              last["action"] == "BUY" and "기록만" in last["reason"], last["reason"])
    finally:
        Config.STRATEGY_MODE = saved_mode
        shutil.rmtree(tmp, ignore_errors=True)


def test_order_recording():
    print("\n[5b] 주문 기록 (기존: record_order() 정의만 있고 호출되는 곳이 없어 실체결도 로그 0건)")
    from config import Config
    from execution_engine import ExecutionEngine
    from market_state import MarketState
    from risk_manager import RiskManager
    from notifier import TelegramNotifier
    from data_recorder import DataRecorder

    async def run_dry_run_buy():
        tmp = tempfile.mkdtemp(prefix="coinbot-orders-")
        try:
            eng = ExecutionEngine.__new__(ExecutionEngine)
            eng.config = Config
            eng.market = MarketState(Config.TARGET_TICKERS)
            eng.risk = RiskManager(state_dir=tempfile.mkdtemp())
            eng.notifier = TelegramNotifier()
            eng.notifier.enabled = False
            eng.active_orders = {}
            eng.balances = {"KRW": {"balance": 1_000_000.0, "locked": 0.0, "avg_buy_price": 0.0}}
            eng._balances_ts = 9e18
            eng.sim_krw = 1_000_000.0
            eng.sim_positions = {}
            eng.client = None
            eng.recorder = DataRecorder(tmp)

            feed_market(eng.market)
            ok = await eng.place_market_buy("KRW-BTC", 10_000)

            path = os.path.join(tmp, "orders", eng.recorder._today() + ".jsonl")
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
            return ok, rows
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    ok, rows = asyncio.run(run_dry_run_buy())
    check("DRY-RUN 매수 성공(기존 동작 유지)", ok)
    check("DRY-RUN 매수가 orders/*.jsonl 에 기록됨", len(rows) == 1, str(rows))
    if rows:
        r = rows[0]
        check("기록 내용 - 종목/방향/상태",
              r["ticker"] == "KRW-BTC" and r["side"] == "bid" and r["state"] == "dry_run",
              str(r))

    async def run_fill_sync():
        import types
        tmp = tempfile.mkdtemp(prefix="coinbot-orders-fill-")
        try:
            eng = ExecutionEngine.__new__(ExecutionEngine)
            eng.config = Config
            eng.market = MarketState(Config.TARGET_TICKERS)
            eng.balances = {}
            eng._balances_ts = 9e18
            eng.recorder = DataRecorder(tmp)
            eng.active_orders = {
                "fake-uuid": {"ticker": "KRW-BTC", "side": "bid",
                             "timestamp": time.time(), "amount_krw": 10_000.0},
            }

            class FakeClient:
                async def get_order(self, order_uuid):
                    return types.SimpleNamespace(state="done", executed_volume=0.0001)

            eng.client = FakeClient()

            async def fake_refresh(force: bool = False):
                pass

            eng._refresh_balances = fake_refresh
            await eng._sync_active_orders()

            path = os.path.join(tmp, "orders", eng.recorder._today() + ".jsonl")
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
            return rows
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    fill_rows = asyncio.run(run_fill_sync())
    check("체결 종료가 orders/*.jsonl 에 기록됨(핵심 - 지금까지 빠졌던 부분)",
          len(fill_rows) == 1 and fill_rows[0]["state"] == "done", str(fill_rows))
    check("체결량이 extra 로 같이 기록됨",
          bool(fill_rows) and fill_rows[0].get("executed_volume") == 0.0001, str(fill_rows))


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

        rm.register_order("KRW-BTC", "bid")
        d = rm.check_buy("KRW-BTC", 10_000, 0, 0, 1_000_000)
        check("쿨다운 중 재주문 거부", not d, d.reason)

        # 일일 상한은 매수에만 건다. 예전에는 공통 게이트에 있어서 상한을
        # 다 쓰면 청산까지 막혔는데, 급락 중에 못 파는 상태가 되므로 위험하다.
        rm4 = RiskManager(state_dir=tempfile.mkdtemp())
        for _ in range(Config.MAX_BUYS_PER_DAY):
            rm4.register_order("KRW-BTC", "bid")
        rm4.last_order_ts = {}          # 쿨다운은 이 검증의 대상이 아니라 비운다
        check("일일 매수 상한 도달 시 매수 차단",
              not rm4.check_buy("KRW-BTC", 10_000, 0, 0, 1_000_000),
              rm4.check_buy("KRW-BTC", 10_000, 0, 0, 1_000_000).reason)
        check("상한을 다 써도 매도(청산)는 통과",
              bool(rm4.check_sell("KRW-BTC", 0.001, 0.001, 100_000_000)),
              f"매수 {rm4.buys_today}/{Config.MAX_BUYS_PER_DAY}")

        # 매도는 상한 카운터를 올리지 않아야 한다(올리면 매도만으로 매수가 막힌다)
        rm5 = RiskManager(state_dir=tempfile.mkdtemp())
        rm5.register_order("KRW-ETH", "ask")
        rm5.register_order("KRW-XRP", "ask")
        check("매도는 일일 매수 카운터를 올리지 않음",
              rm5.buys_today == 0 and rm5.orders_today == 2,
              f"매수 {rm5.buys_today} / 전체 {rm5.orders_today}")

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

    import time as _t
    # 주기 대비 대기 여유를 넉넉히 준다. 윈도우 타이머 해상도가 약 15ms 라
    # 0.05초 주기에 0.06초만 기다리면 테스트 자체가 간헐적으로 깨진다.
    INTERVAL = 0.05
    s = Scheduler()
    check("첫 호출은 미발동(한 주기 대기)", not s.due("x", INTERVAL))
    _t.sleep(INTERVAL * 3)
    check("주기 경과 후 발동", s.due("x", INTERVAL))
    check("연속 호출은 미발동", not s.due("x", INTERVAL))
    # 오래 밀려도 반드시 1회 발동하고, 몰아서 중복 발동하지는 않는지
    _t.sleep(INTERVAL * 10)
    check("크게 밀려도 발동 보장", s.due("x", INTERVAL))
    check("밀린 주기를 몰아서 중복 발동하지 않음", not s.due("x", INTERVAL))


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
        eng.recorder = None

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
# 가격행동 (Naked Forex) 전략
# ---------------------------------------------------------------------------
def _bars(ohlc, period=3600, t0=1_700_000_000.0):
    from price_action import Bars
    rows = [(t0 + k * period, o, h, l, c, 1.0) for k, (o, h, l, c) in enumerate(ohlc)]
    return Bars.from_rows(rows, period)


def _downtrend_then(tail, n=30, start=116.0, step=0.5):
    """완만한 하락(저가가 존 위) 뒤에 tail 봉들을 붙인다."""
    ohlc, c = [], start
    for _ in range(n):
        o, c = c, c - step
        ohlc.append((o, max(o, c) + 0.3, min(o, c) - 0.3, c))
    return ohlc + list(tail)


def _zone(price=100.0, half=0.5, touches=3):
    from price_action import Zone
    return Zone(price, price - half, price + half, touches)


def _uptrend_pause_kangaroo():
    ohlc = []
    for j in range(60):
        c = 100 + 0.5 * j
        o = c - 0.4
        ohlc.append((o, c + 0.2, o - 0.2, c))
    for _ in range(5):                                   # 쉬어가기
        ohlc.append((129.6, 130.0, 129.4, 129.8))
    ohlc.append((129.7, 130.0, 127.5, 129.9))            # 추세 캥거루
    return ohlc


def test_price_action_patterns():
    print("\n[11] 가격행동 패턴 (책 규칙의 숫자화)")
    import numpy as np
    from price_action import (kangaroo_tail, big_shadow, wammie, last_kiss,
                              trendy_kangaroo, find_zones, evaluate_bar, Zone)

    z = [_zone(100.0)]
    prev = (102.5, 103.0, 101.8, 102.2)
    good = _bars(_downtrend_then([prev, (102.4, 103.0, 99.8, 102.8)]))
    i = len(good) - 1
    check("캥거루 꼬리: 존 위 + 위쪽1/3 + 왼쪽여백", kangaroo_tail(good, i, z) is not None)
    mid = _bars(_downtrend_then([prev, (102.4, 103.0, 99.8, 101.0)]))
    check("캥거루 꼬리: 종가가 중간이면 아님", kangaroo_tail(mid, i, z) is None)
    off = _bars(_downtrend_then([prev, (102.4, 103.0, 101.2, 102.8)]))
    check("캥거루 꼬리: 존에 안 닿으면 아님", kangaroo_tail(off, i, z) is None)
    runaway = _bars(_downtrend_then([prev, (103.4, 104.0, 99.8, 103.8)]))
    check("캥거루 꼬리: 시가·종가가 직전 봉 밖(폭주)이면 아님", kangaroo_tail(runaway, i, z) is None)
    # 직전 봉들의 저가가 이미 꼬리 저가 아래 = 시장이 최근에 다녀간 가격
    no_room = _bars([(101.0, 101.2, 99.6, 101.0)] * 10 + [prev, (102.4, 103.0, 99.8, 102.8)])
    check("캥거루 꼬리: 왼쪽 여백 없으면 아님",
          kangaroo_tail(no_room, len(no_room) - 1, z) is None)

    bs_prev = (102.2, 102.6, 101.6, 101.9)
    bs = _bars(_downtrend_then([bs_prev, (101.8, 103.0, 99.9, 102.9)]))
    check("빅 섀도: 감싸기 + 고가 근처 종가 + 존", big_shadow(bs, len(bs) - 1, z) is not None)
    bs_mid = _bars(_downtrend_then([bs_prev, (101.8, 103.0, 99.9, 101.5)]))
    check("빅 섀도: 종가가 중간이면 아님", big_shadow(bs_mid, len(bs_mid) - 1, z) is None)
    bs_inside = _bars(_downtrend_then([bs_prev, (101.8, 102.5, 99.9, 102.4)]))
    check("빅 섀도: 고가를 못 넘으면(감싸기 아님) 아님",
          big_shadow(bs_inside, len(bs_inside) - 1, z) is None)

    # 와미: 첫 터치 99.7 -> 반등 -> 두 번째 터치 100.1(더 높음) -> 강한 양봉
    w = [(105.0, 105.6, 104.4, 105.0)] * 30
    w += [(101.5, 101.6, 99.7, 100.4)]                                  # 30 첫 터치
    w += [(100.4 + k * 0.4, 100.9 + k * 0.4, 100.7 + k * 0.4, 100.8 + k * 0.4) for k in range(6)]
    w += [(103.0, 104.2, 102.8, 103.8), (103.8, 104.0, 102.6, 102.8), (102.8, 102.9, 101.6, 101.8),
          (101.8, 101.9, 100.9, 101.0), (101.0, 101.1, 100.7, 100.8)]
    w += [(100.8, 100.9, 100.1, 100.6)]                                 # 42 두 번째 터치
    w += [(100.6, 101.8, 100.4, 101.7)]                                 # 43 신호
    wb = _bars(w)
    res = wammie(wb, len(wb) - 1, z)
    check("와미: 두 번째 저점이 더 높은 이중바닥", res is not None and res["second"] > res["first"],
          str(res and {k: res[k] for k in ("first", "second", "gap")}))
    w_low = list(w)
    w_low[42] = (100.8, 100.9, 99.5, 100.6)                             # 두 번째가 더 낮음
    check("와미: 두 번째 저점이 더 낮으면 아님", wammie(_bars(w_low), len(w) - 1, z) is None)

    # 라스트 키스: 횡보 박스 -> 상단 돌파 -> 상단 되돌림 양봉
    lk, prev_c = [], 102.0
    for j in range(41):
        c = 102.0 + 1.5 * np.sin(2 * np.pi * j / 10)
        lk.append((prev_c, max(prev_c, c) + 0.5, min(prev_c, c) - 0.5, c))
        prev_c = c
    lk.append((prev_c, 105.3, prev_c - 0.2, 105.0))                     # 41 돌파
    lk += [(105.0, 106.4, 105.5, 106.0), (106.0, 106.6, 105.6, 105.8), (105.8, 106.0, 105.4, 105.6)]
    lk.append((105.4, 105.9, 104.2, 105.7))                              # 45 키스
    lkb = _bars(lk)
    res = last_kiss(lkb, len(lkb) - 1)
    check("라스트 키스: 박스 돌파 후 상단 되돌림", res is not None, str(res))
    lk_bad = list(lk)
    lk_bad[43] = (106.0, 106.1, 103.0, 103.2)                            # 박스 안 마감 = 가짜 돌파
    check("라스트 키스: 돌파 후 박스 안 마감이면 아님",
          last_kiss(_bars(lk_bad), len(lk_bad) - 1) is None)

    tk = _bars(_uptrend_pause_kangaroo())
    check("추세 캥거루: 상승추세 + 쉬어가기 + 꼬리 이탈", trendy_kangaroo(tk, len(tk) - 1) is not None)
    flat = [(100.0, 100.5, 99.5, 100.1)] * 60 + _uptrend_pause_kangaroo()[60:]
    check("추세 캥거루: 추세가 없으면 아님", trendy_kangaroo(_bars(flat), len(flat) - 1) is None)

    # 존: 100 근처에서 3번 꺾인 톱니
    saw = []
    for rep in range(4):
        for c in [104, 102, 100, 102, 104, 106, 108, 106]:
            saw.append((c, c + 0.4, c - 0.4, c))
    zones = find_zones(_bars(saw), pivot_k=2)
    check("존: 반복해서 꺾인 가격에 존이 생김",
          any(abs(zz.price - 100) < 0.6 and zz.touches >= 3 for zz in zones),
          str([(round(zz.price, 1), zz.touches) for zz in zones]))

    # 미래 봉 미참조: i 까지 자른 데이터와 전체 데이터의 판정이 같아야 한다
    sig_full = evaluate_bar(good, i, z)
    sig_cut = evaluate_bar(good.upto(i + 1), i, z)
    check("판정이 미래 봉을 참조하지 않음",
          [s.to_dict() for s in sig_full] == [s.to_dict() for s in sig_cut])
    ks = [s for s in sig_full if s.pattern == "kangaroo"]
    check("신호: 매수스톱=고가 위, 손절=저가 아래",
          bool(ks) and ks[0].entry > 103.0 and ks[0].stop < 99.8,
          str(ks and (ks[0].entry, ks[0].stop)))


def _sig(entry=101.0, stop=99.0, targets=(105.0, 108.0), pattern="kangaroo", soft=None):
    return {"pattern": pattern, "direction": 1, "kind": "entry", "bar_time": -3600.0,
            "close_time": 0.0, "entry": entry, "stop": stop, "targets": list(targets),
            "soft_exit": soft, "zone": None, "features": {"range_atr": 1.2}}


def test_trade_state_machine():
    print("\n[12] 가격행동 거래 상태기계 (매수스톱·구조적 손절·11장 청산)")
    from naked_strategy import Trade, ROUND_TRIP_COST

    tr = Trade("KRW-BTC", _sig(), "zone", 3600)
    tr.on_bar(0, 100.5, 101.5, 100.0, 101.2)
    check("매수스톱 체결 (고가가 진입가 돌파)", tr.status == "open" and tr.fill_px == 101.0)
    tr.on_bar(3600, 101.2, 105.2, 101.0, 104.0)
    r = tr.result()
    check("존 목표가 청산", tr.status == "closed" and tr.exit_reason == "target",
          f"{tr.exit_reason}")
    check("순수익 = 총수익 - 왕복비용",
          abs(r["net_ret"] - (105 / 101 - 1 - ROUND_TRIP_COST)) < 1e-12, f"{r['net_ret']:.5f}")

    tr = Trade("KRW-BTC", _sig(), "zone", 3600)
    tr.on_bar(0, 100.0, 100.8, 98.9, 99.5)
    check("진입 전 손절선 도달 -> 신호 취소 (13장)",
          tr.status == "cancelled" and tr.exit_reason == "stop_before_entry")

    tr = Trade("KRW-BTC", _sig(), "zone", 3600)
    tr.on_bar(0, 100.5, 101.5, 98.5, 99.0)
    check("같은 봉 진입+손절은 손실로 (보수적)",
          tr.status == "closed" and tr.exit_reason == "stop" and tr.result()["net_ret"] < 0)

    tr = Trade("KRW-BTC", _sig(), "zone", 3600)
    tr.on_bar(0, 100.5, 101.5, 100.0, 101.2)
    tr.on_bar(3600, 101.0, 101.5, 98.0, 98.2)
    check("손절", tr.exit_reason == "stop" and tr.fills[-1][1] == 99.0)

    tr = Trade("KRW-BTC", _sig(), "zone", 3600)
    tr.on_bar(0, 100.5, 101.5, 100.0, 101.2)
    tr.on_bar(3600, 97.0, 97.5, 96.0, 96.5)
    check("갭 하락이면 시가에 청산", tr.fills[-1][1] == 97.0)

    tr = Trade("KRW-BTC", _sig(), "zone", 3600, valid_bars=2)
    tr.on_bar(3600, 100.0, 100.5, 99.5, 100.2)
    tr.on_bar(7200, 100.0, 102.0, 99.5, 101.5)
    check("매수스톱 유효기간 경과 -> 취소", tr.status == "cancelled" and tr.exit_reason == "expired")

    tr = Trade("KRW-BTC", _sig(), "split", 3600)
    tr.on_bar(0, 100.5, 101.5, 100.0, 101.2)
    tr.on_bar(3600, 101.2, 105.5, 101.2, 104.0)
    check("분할: 첫 존에서 절반 + 손절 본전", tr.remaining == 0.5 and tr.stop == 101.0,
          f"rem={tr.remaining} stop={tr.stop}")
    tr.on_bar(7200, 104.0, 104.5, 100.5, 100.8)
    check("분할: 나머지는 본전에서 청산", tr.status == "closed" and tr.fills[-1][1] == 101.0)

    check("목표 존 없으면 auto -> 3봉 추적",
          Trade("KRW-BTC", _sig(targets=()), "auto", 3600).mode == "three_bar")
    check("목표 존 있으면 auto -> 존 청산",
          Trade("KRW-BTC", _sig(), "auto", 3600).mode == "zone")

    # 3봉 추적: 봉이 오를수록 손절이 최근 3봉 저가 아래로 따라 올라간다
    tb = _bars([(100, 101, 99, 100.5)] * 20 + [(101, 102, 100.5, 101.8), (101.8, 103, 101.5, 102.8),
                                               (102.8, 104, 102.5, 103.8), (103.8, 105, 103.5, 104.8)])
    tr = Trade("KRW-BTC", _sig(targets=()), "three_bar", 3600)
    tr.mark_filled(101.0, float(tb.t[20]))
    for k in range(20, 24):
        tr.on_bar_close(tb, k, False)
    # 최근 3봉 저가(101.5, 102.5, 103.5)의 최저 - 0.05 ATR = 진입가(101) 위로 올라와야
    check("3봉 추적 손절 상승 (최근 3봉 최저가 아래)", 101.0 < tr.stop < 101.5, f"stop={tr.stop:.3f}")

    tr = Trade("KRW-BTC", _sig(), "ladder", 3600)
    tr.mark_filled(101.0, float(tb.t[20]))
    lb = _bars([(100, 101, 99, 100.5)] * 20 + [(101, 105.5, 100.8, 105.0)])
    tr.on_bar_close(lb, 20, False)
    check("사다리: 첫 존 도달 -> 손절 본전", tr.stop == 101.0 and tr.ladder_step == 1)

    tr = Trade("KRW-BTC", _sig(soft=102.0), "zone", 3600)
    tr.mark_filled(103.0, float(tb.t[20]))
    sb = _bars([(100, 101, 99, 100.5)] * 20 + [(103, 103.5, 101.5, 101.8)])
    check("라스트 키스: 종가가 박스 안이면 청산 사유",
          tr.on_bar_close(sb, 20, False) == "back_in_box")

    st = Trade.from_state(Trade("KRW-BTC", _sig(), "zone", 3600).to_state())
    check("거래 상태 직렬화 왕복", st.entry == 101.0 and st.targets == [105.0, 108.0]
          and st.status == "pending")


def _naked_cfg(**over):
    import types
    base = dict(TARGET_TICKERS=["KRW-BTC", "KRW-ETH"], NAKED_LIVE_TICKERS=["KRW-BTC", "KRW-ETH"],
                STRATEGY_MODE="naked", NAKED_LIVE=True, NAKED_LIVE_ALTS=True,
                NAKED_PATTERNS=("kangaroo", "big_shadow", "wammie", "last_kiss", "trendy_kangaroo"),
                NAKED_LIVE_PATTERNS=("trendy_kangaroo",), NAKED_EXIT_MODE="zone",
                NAKED_MIN_RR=1.0, NAKED_ENTRY_VALID_BARS=2, NAKED_MAX_HOLD_BARS=72,
                NAKED_RISK_PER_TRADE_KRW=1000.0, MAX_POSITION_KRW=50_000.0,
                MIN_ORDER_KRW=5_000.0, NAKED_ALTS_ENABLED=True, NAKED_TF_MIN=60,
                NAKED_ZONE_TF_MIN=240, NAKED_ZONE_BARS=200)
    base.update(over)
    return types.SimpleNamespace(**base)


def test_naked_live_execution():
    print("\n[13] 가격행동 라이브 연결 (매수스톱 체결·긴급 청산·알트 구독·영속화)")
    from naked_strategy import NakedStrategy, PaperBook, EXIT_MODES

    tmp = tempfile.mkdtemp(prefix="coinbot-naked-")
    try:
        calls = []
        watched = []

        async def buy(t, krw):
            calls.append(("buy", t, round(krw)))
            return True

        async def sell(t, vol):
            calls.append(("sell", t, vol))
            return True

        price = {"v": None}
        ns = NakedStrategy(tmp, feed=None, cfg=_naked_cfg(), buy=buy, sell=sell,
                           price_of=lambda t: price["v"], watch=watched.append,
                           unwatch=lambda t: watched.append("-" + t))
        # 라이브 대기는 실제 시각으로 만료되므로 방금 마감된 봉처럼 만든다
        ohlc = _uptrend_pause_kangaroo()
        bars = _bars(ohlc, t0=time.time() - len(ohlc) * 3600)
        i = len(bars) - 1
        ns.process_bar("KRW-BTC", bars, i, [])
        lt = ns.live.get("KRW-BTC")
        check("라이브 패턴 신호 -> 라이브 대기 생성", lt is not None and lt.status == "pending")
        check("같은 신호가 종이 매매 5가지 청산방식으로도 굴러감",
              len(ns.book.open.get("KRW-BTC", [])) == len(EXIT_MODES))

        async def ticks(prices):
            for p in prices:
                price["v"] = p
                await ns.on_tick(can_enter=True)

        entry, stop = lt.entry, lt.stop
        asyncio.run(ticks([entry - 0.05]))
        check("진입가 아래에서는 주문 없음", calls == [])
        asyncio.run(ticks([entry + 0.01]))
        check("매수스톱 돌파 -> 시장가 매수", calls and calls[0][0] == "buy", str(calls))
        fill = entry + 0.01
        exp_krw = min(1000 / ((fill - stop) / fill + 0.0012), 50_000)
        check("고정 위험 사이징 (실제 매수가 기준, 손절 시 1,000원 손실)",
              abs(calls[0][2] - max(exp_krw, 5000)) <= 1, f"{calls[0][2]} vs {exp_krw:.0f}")
        asyncio.run(ticks([stop - 0.01]))
        check("손절선 이탈 -> 매도", calls[-1][0] == "sell" and "KRW-BTC" not in ns.live,
              str(calls[-1]))
        trades_path = glob.glob(os.path.join(tmp, "naked", "trades", "*.jsonl"))
        recs = [json.loads(l) for p in trades_path for l in open(p, encoding="utf-8")]
        check("라이브 거래 결과가 기록됨",
              any(r["source"] == "live" and r["exit_reason"] == "stop" for r in recs))

        # 커널 패턴이 라이브 대상이 아니면 종이 매매만
        ns2 = NakedStrategy(tmp + "2", feed=None, cfg=_naked_cfg(NAKED_LIVE_PATTERNS=("kangaroo",)),
                            buy=buy, sell=sell, price_of=lambda t: None)
        ns2.process_bar("KRW-BTC", bars, i, [])
        check("라이브 패턴이 아니면 라이브 대기 없음", "KRW-BTC" not in ns2.live)

        # 알트: 신호 시 구독, 끝나면 해제
        calls.clear()
        ns.process_bar("KRW-ALT", bars, i, [])
        check("알트 라이브 신호 -> 시세 구독", watched and watched[-1] == "KRW-ALT", str(watched))
        ns.save()
        ns_r = NakedStrategy(tmp, feed=None, cfg=_naked_cfg(), buy=buy, sell=sell,
                             price_of=lambda t: price["v"])
        check("재시작 후 라이브 대기 복원", "KRW-ALT" in ns_r.live
              and ns_r.live["KRW-ALT"].status == "pending")
        check("재시작 후 종이 거래 복원", len(ns_r.book.all_open()) == len(ns.book.all_open()))
        alt = ns.live["KRW-ALT"]
        asyncio.run(ticks([alt.entry + 0.01, alt.stop - 0.01]))
        check("알트 거래 종료 -> 구독 해제", watched[-1] == "-KRW-ALT", str(watched))
        check("알트 청산은 먼지까지 (보유량의 101% 요청)",
              calls[-1][0] == "sell" and calls[-1][2] > 0, str(calls[-1]))

        ns3 = NakedStrategy(tmp + "3", feed=None, cfg=_naked_cfg(NAKED_LIVE_ALTS=False),
                            buy=buy, sell=sell, price_of=lambda t: None)
        ns3.process_bar("KRW-ALT", bars, i, [])
        check("알트 라이브 꺼져 있으면 섀도만", "KRW-ALT" not in ns3.live)

        # 서킷브레이커 중에는 진입하지 않는다
        ns4 = NakedStrategy(tmp + "4", feed=None, cfg=_naked_cfg(), buy=buy, sell=sell,
                            price_of=lambda t: price["v"])
        ns4.process_bar("KRW-BTC", bars, i, [])
        calls.clear()
        price["v"] = ns4.live["KRW-BTC"].entry + 0.01
        asyncio.run(ns4.on_tick(can_enter=False))
        check("서킷브레이커 중 매수스톱 보류", calls == [] and ns4.live["KRW-BTC"].status == "pending")

        # 종이 장부: 종목당 청산방식별 거래 하나 (라이브와 같은 제약)
        pb = PaperBook(("zone",), ("trendy_kangaroo",))
        pb.process_bar("KRW-BTC", bars, i, [])
        pb.process_bar("KRW-BTC", bars, i, [])
        check("종이 장부 종목당 1거래", len(pb.open["KRW-BTC"]) == 1)
    finally:
        for sfx in ("", "2", "3", "4"):
            shutil.rmtree(tmp + sfx, ignore_errors=True)


def test_naked_infra():
    print("\n[14] 가격행동 부대설비 (캔들 마감·알트 선별·긴급청산·구독·검증 게이트)")
    import types
    from candle_feed import candles_to_bars, AltUniverse
    from risk_manager import RiskManager

    C = lambda t, o, h, l, c: types.SimpleNamespace(
        candle_date_time_utc=t, opening_price=o, high_price=h, low_price=l,
        trade_price=c, candle_acc_trade_price=1.0)
    from datetime import datetime, timezone
    now = datetime(2026, 9, 24, 2, 30, tzinfo=timezone.utc).timestamp()
    raw = [C("2026-09-24T02:00:00", 3, 3, 3, 3), C("2026-09-24T01:00:00", 2, 2, 2, 2),
           C("2026-09-24T00:00:00", 1, 1, 1, 1)]
    b = candles_to_bars(raw, 60, now)
    check("형성 중인 봉은 버리고 시간순 정렬", list(b.c) == [1.0, 2.0], str(list(b.c)))

    P = lambda m, warn=None, **caution: types.SimpleNamespace(
        market=m, market_warning=warn,
        market_event=types.SimpleNamespace(warning=False, caution=types.SimpleNamespace(**caution)))
    T = lambda m, v: types.SimpleNamespace(market=m, acc_trade_price_24h=v, signed_change_rate=0.1)
    pairs = [P("KRW-AAA"), P("KRW-BBB", warn="CAUTION"), P("KRW-CCC", trading_volume_soaring=True),
             P("KRW-USDT"), P("KRW-DDD"), P("KRW-BTC"), P("BTC-EEE"), P("KRW-FFF")]
    tks = [T("KRW-AAA", 9e9), T("KRW-BBB", 9e9), T("KRW-CCC", 9e9), T("KRW-USDT", 9e9),
           T("KRW-DDD", 1e9), T("KRW-BTC", 9e9), T("KRW-FFF", 8e9)]
    u = AltUniverse(None, exclude=["KRW-BTC"], top_n=5, min_trade_krw=5e9, max_spread=0.002)
    sel = u.select(pairs, tks, spreads={"KRW-FFF": 0.01})
    check("알트 선별: 유의/주의(펌프)·스테이블·저유동·넓은 스프레드·코어 제외",
          sel == ["KRW-AAA"], str(sel))

    rm = RiskManager(state_dir=tempfile.mkdtemp())
    rm.register_order("KRW-BTC", "bid")
    check("일반 매도는 재주문 쿨다운에 걸림",
          not rm.check_sell("KRW-BTC", 1.0, 1.0, 10_000.0))
    check("손절/목표 청산(urgent)은 쿨다운 건너뜀",
          bool(rm.check_sell("KRW-BTC", 1.0, 1.0, 10_000.0, urgent=True)))
    check("urgent 여도 보유량 초과 매도는 차단",
          not rm.check_sell("KRW-BTC", 2.0, 1.0, 10_000.0, urgent=True))

    from execution_engine import ExecutionEngine
    from market_state import MarketState
    from config import Config
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.config = Config
    eng.market = MarketState(Config.TARGET_TICKERS)
    eng.extra_tickers = set()
    subs = []
    eng.ws_feed = types.SimpleNamespace(set_tickers=lambda t: subs.append(list(t)))
    eng.sim_positions, eng.balances = {}, {}
    eng.watch("KRW-ALT")
    check("알트 구독: 시세 상태 + 웹소켓 목록에 추가",
          eng.market.get("KRW-ALT") is not None and "KRW-ALT" in subs[-1])
    check("알트가 노출 회계에 포함", "KRW-ALT" in eng.tracked_tickers())
    eng.unwatch("KRW-ALT")
    check("보유 없으면 구독 해제", "KRW-ALT" not in eng.tracked_tickers())

    from validate_naked import evaluate, write_report, check_gate
    rng = np.random.default_rng(3)
    trades = []
    for k in range(400):
        for v in ("zone", "split", "ladder", "three_bar"):
            edge = 0.006 if v == "ladder" else 0.0
            trades.append({"ticker": "KRW-X", "pattern": "trendy_kangaroo", "variant": v,
                           "filled": True, "status": "closed", "signal_ts": k * 36000.0,
                           "exit_ts": k * 36000.0 + 7200, "net_ret": rng.normal(edge, 0.01),
                           "r_net": None, "is_alt": True, "exit_reason": "stop"})
    rep = evaluate(trades, ("trendy_kangaroo",), "ladder", paper=[])
    check("백테스트만으로는 통과 못 함 (표본 외 종이매매 필요)", rep["passed"] is False
          and rep["checks"]["거래 100건 이상"] is True, str(rep["checks"]))
    paper = [dict(t, source="paper") for t in trades if t["variant"] == "ladder"][:40]
    rep2 = evaluate(trades, ("trendy_kangaroo",), "ladder", paper=paper)
    check("표본 외까지 양수면 통과", rep2["passed"], str(rep2["checks"]))
    d = tempfile.mkdtemp()
    write_report(d, rep2, {"source": "test"})
    check("검증 게이트 통과", check_gate(d, 30)[0])
    write_report(d, rep, {"source": "test"})
    ok, why = check_gate(d, 30)
    check("검증 게이트 미통과 사유 제시", not ok and "미통과" in why, why)
    check("검증 리포트 없으면 게이트 닫힘", not check_gate(tempfile.mkdtemp(), 30)[0])


def test_naked_review_fixes():
    print("\n[15] 가격행동 최종 검토 수정분 (표본 일치·경쟁 조건·게이트 재개방·자동 검증)")
    import types
    from naked_strategy import NakedStrategy, PaperBook, Trade
    from validate_naked import (non_overlap, select, evaluate, save_backtest, reevaluate,
                                auto_validate, check_gate, write_report, backtest_params,
                                backtest_meta)
    from candle_feed import CandleFeed

    ohlc = _uptrend_pause_kangaroo()
    fresh = _bars(ohlc, t0=time.time() - len(ohlc) * 3600)
    i = len(fresh) - 1

    # (1) 종이 장부는 패턴끼리 막지 않는다 - 라이브 패턴의 표본이 다른 패턴 때문에 빠지면 안 됨
    pb = PaperBook(("zone",), ("kangaroo", "trendy_kangaroo"))
    other = Trade("KRW-BTC", dict(_sig(), bar_time=float(fresh.t[i]),
                                  close_time=fresh.close_time(i)), "zone", 3600)
    pb.open["KRW-BTC"] = [other]
    pb.process_bar("KRW-BTC", fresh, i, [])
    pats = sorted(tr.pattern for tr in pb.open["KRW-BTC"])
    check("다른 패턴 종이 거래가 열려 있어도 새 패턴 신호는 기록",
          pats == ["kangaroo", "trendy_kangaroo"], str(pats))
    pb.process_bar("KRW-BTC", fresh, i, [])
    check("같은 패턴·청산방식은 종목당 하나", len(pb.open["KRW-BTC"]) == 2)

    # (2) 여러 패턴을 함께 쓰는 전략은 평가 때 '종목당 1거래' 적용
    T = lambda p, s, e, filled=True, st="closed": {
        "ticker": "KRW-X", "pattern": p, "variant": "zone", "signal_ts": s, "exit_ts": e,
        "filled": filled, "status": st, "net_ret": 0.01, "r_net": None,
        "is_alt": True, "exit_reason": "target"}
    tr_all = [T("kangaroo", 0, 10), T("trendy_kangaroo", 5, 20), T("trendy_kangaroo", 12, 30),
              T("kangaroo", 40, 41, False, "cancelled"), T("trendy_kangaroo", 40.5, 50)]
    kept = [(t["pattern"], t["signal_ts"]) for t in non_overlap(tr_all)]
    check("겹치는 거래 제외 (보유 중·매수스톱 대기 중 모두 점유)",
          kept == [("kangaroo", 0), ("trendy_kangaroo", 12), ("kangaroo", 40)], str(kept))
    check("단일 패턴 전략은 겹침 제거 없이 전부",
          len(select(tr_all, "zone", ("trendy_kangaroo",))) == 3)
    check("복수 패턴 전략은 체결·종료된 비겹침 거래만",
          len(select(tr_all, "zone", ("kangaroo", "trendy_kangaroo"))) == 2)

    # (3) 주문 응답 대기 중(busy)에는 스캔이 라이브 거래를 만료시키지 않는다
    tmp = tempfile.mkdtemp(prefix="coinbot-naked-rv-")
    try:
        calls = []

        async def buy(t, krw):
            calls.append(("buy", t))
            return True

        async def sell(t, vol):
            calls.append(("sell", t))
            return True

        px = {"v": None}
        ns = NakedStrategy(tmp, feed=None, cfg=_naked_cfg(), buy=buy, sell=sell,
                           price_of=lambda t: px["v"])
        ns.process_bar("KRW-BTC", fresh, i, [])
        lt = ns.live["KRW-BTC"]
        lt.expire_ts = 0.0
        lt.busy = True
        ns.process_bar("KRW-BTC", fresh, i, [])
        check("주문 응답 대기 중에는 만료 처리 안 함 (고아 포지션 방지)",
              "KRW-BTC" in ns.live and lt.status == "pending")
        lt.busy = False
        lt.expire_ts = time.time() + 3600

        # (4) 게이트가 닫히면 대기 중 매수스톱은 취소, 주문 없음
        ns.live_enabled = False
        px["v"] = lt.entry + 0.01
        asyncio.run(ns.on_tick(can_enter=True))
        check("게이트 닫힘 -> 대기 매수스톱 취소 (live_disabled)",
              calls == [] and lt.exit_reason == "live_disabled" and "KRW-BTC" not in ns.live,
              f"{calls} {lt.exit_reason}")

        # 게이트가 닫혀도 보유 중인 거래는 손절까지 관리
        ns.live_enabled = True
        ns.process_bar("KRW-ETH", fresh, i, [])
        eth = ns.live["KRW-ETH"]
        px["v"] = eth.entry + 0.01
        asyncio.run(ns.on_tick(can_enter=True))
        ns.live_enabled = False
        px["v"] = eth.stop - 0.01
        asyncio.run(ns.on_tick(can_enter=True))
        check("게이트 닫혀도 보유 거래 손절은 나감",
              calls[-1] == ("sell", "KRW-ETH") and eth.status == "closed", str(calls))

        # (5) 재시작 후 밀린 봉 재생: 종이 매매는 하되 옛 신호로 주문하지 않는다
        class Feed:
            def __init__(self, b):
                self.b = b

            async def get_bars(self, ticker, unit, count=200):
                return self.b

        old = _bars(ohlc)                      # 2023년 봉 = 오래전에 마감
        ns2 = NakedStrategy(tmp + "2", feed=Feed(old), cfg=_naked_cfg(), buy=buy, sell=sell,
                            price_of=lambda t: None)
        ns2.last_bar["KRW-BTC"] = float(old.t[i - 1])
        asyncio.run(ns2._scan_ticker("KRW-BTC"))
        sig_files = glob.glob(os.path.join(tmp + "2", "naked", "signals", "*.jsonl"))
        sigs = [json.loads(l) for p in sig_files for l in open(p, encoding="utf-8")]
        check("재생 봉 신호는 종이 매매만 (라이브 주문 없음)",
              "KRW-BTC" not in ns2.live and len(ns2.book.open.get("KRW-BTC", [])) > 0
              and sigs and "재생" in sigs[-1]["why"], str([s["why"] for s in sigs]))

        # (6) 받아온 200봉보다 긴 공백 -> 그 종목 종이 거래는 결과를 믿을 수 없어 취소
        ns3 = NakedStrategy(tmp + "3", feed=Feed(fresh), cfg=_naked_cfg(), buy=buy, sell=sell,
                            price_of=lambda t: None)
        stale_tr = Trade("KRW-BTC", _sig(), "zone", 3600)
        ns3.book.open["KRW-BTC"] = [stale_tr]
        ns3.last_bar["KRW-BTC"] = float(fresh.t[0]) - 30 * 86400
        asyncio.run(ns3._scan_ticker("KRW-BTC"))
        check("데이터 공백 -> 종이 거래 취소 (data_gap)",
              stale_tr.status == "cancelled" and stale_tr.exit_reason == "data_gap")
        check("공백 뒤에도 방금 마감된 봉 신호는 라이브", "KRW-BTC" in ns3.live)
        ns3.live["KRW-ALT"] = Trade("KRW-ALT", _sig(), "zone", 3600, live=True, is_alt=True)
        check("라이브 거래 종목은 유니버스에 없어도 스캔 대상", "KRW-ALT" in ns3.tickers())

        # (6b) 클럭 타임아웃으로 on_tick 이 취소돼도 접수된 주문의 상태 갱신은 끝난다
        buys = []

        async def slow_buy(t, krw):
            buys.append(t)                    # 주문은 접수됐고
            await asyncio.sleep(0.2)          # 응답(체결 알림 등)이 느리다
            return True

        ns5 = NakedStrategy(tmp + "5", feed=None, cfg=_naked_cfg(), buy=slow_buy, sell=sell,
                            price_of=lambda t: px["v"])
        ns5.process_bar("KRW-BTC", fresh, i, [])
        px["v"] = ns5.live["KRW-BTC"].entry + 0.01

        async def clock_like():
            try:
                await asyncio.wait_for(ns5.on_tick(True), timeout=0.05)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.3)
            await ns5.on_tick(True)           # 다음 틱: 이미 체결됐으니 다시 사면 안 됨
            await asyncio.sleep(0.3)

        asyncio.run(clock_like())
        check("on_tick 취소돼도 체결 상태 반영 + 이중 매수 없음",
              buys == ["KRW-BTC"] and ns5.live["KRW-BTC"].status == "open", str(buys))

        async def shutdown_then_tick():
            await ns5.drain(1.0)
            px["v"] = ns5.live["KRW-BTC"].stop - 0.01
            await ns5.on_tick(True)

        calls.clear()
        asyncio.run(shutdown_then_tick())
        check("종료 절차(drain) 뒤에는 새 주문을 내지 않음", calls == [], str(calls))
    finally:
        for sfx in ("", "2", "3", "5"):
            shutil.rmtree(tmp + sfx, ignore_errors=True)

    # (7) 봉이 안 생기는 종목에 20초마다 재요청하지 않는다
    stamp = time.strftime("%Y-%m-%dT%H:00:00", time.gmtime(time.time() - 5 * 3600))
    candle = types.SimpleNamespace(candle_date_time_utc=stamp, opening_price=1, high_price=1,
                                   low_price=1, trade_price=1, candle_acc_trade_price=1)
    n_calls = {"n": 0}

    async def list_minutes(unit, **kw):
        n_calls["n"] += 1
        return [candle]

    fake = types.SimpleNamespace(candles=types.SimpleNamespace(list_minutes=list_minutes))
    feed = CandleFeed(client=fake)

    async def twice():
        await feed.get_bars("KRW-X", 60)
        await feed.get_bars("KRW-X", 60)

    asyncio.run(twice())
    check("캔들 재요청 최소 간격 (거래 없는 종목 스팸 방지)", n_calls["n"] == 1, str(n_calls))
    check("라이브 스캔 + 자동 백테스트 요청 합이 업비트 한도(초당 10) 이하",
          CandleFeed.LIVE_PER_SECOND + CandleFeed.BACKTEST_PER_SECOND < 10)

    # (8) 자동 검증: 백테스트가 신선하면 네트워크 없이 표본 외 종이 매매만 합쳐 재평가
    d = tempfile.mkdtemp(prefix="coinbot-naked-val-")
    try:
        cfg = _naked_cfg(NAKED_EXIT_MODE="ladder", NAKED_PATTERNS=("trendy_kangaroo",))
        rng = np.random.default_rng(5)
        bt = []
        for k in range(300):
            for v in ("zone", "split", "ladder", "three_bar"):
                bt.append({"ticker": "KRW-X", "pattern": "trendy_kangaroo", "variant": v,
                           "filled": True, "status": "closed", "signal_ts": k * 36000.0,
                           "exit_ts": k * 36000.0 + 7200,
                           "net_ret": rng.normal(0.006 if v == "ladder" else 0.0, 0.01),
                           "r_net": None, "is_alt": True, "exit_reason": "stop",
                           "source": "backtest"})
        save_backtest(d, bt, {"params": backtest_params(cfg)})
        res = asyncio.run(auto_validate(d, cfg, refresh_days=30, days=180))
        check("백테스트가 신선하면 재실행 안 함", res["backtest_refreshed"] is False)
        check("결과를 바꾸는 설정이 바뀌면 백테스트는 낡은 것",
              backtest_meta(d)["params"] != backtest_params(_naked_cfg(NAKED_MIN_RR=2.0)))
        check("표본 외 종이 매매 없으면 미통과", res["report"]["passed"] is False)
        os.makedirs(os.path.join(d, "naked", "trades"), exist_ok=True)
        with open(os.path.join(d, "naked", "trades", "2026-09-25.jsonl"), "w", encoding="utf-8") as f:
            for k in range(40):
                f.write(json.dumps(dict(bt[2], signal_ts=2e7 + k * 36000.0,
                                        exit_ts=2e7 + k * 36000.0 + 7200,
                                        net_ret=0.004, source="paper")) + "\n")
        rep = reevaluate(d, cfg)
        check("표본 외 종이 매매가 쌓이면 재평가로 통과",
              rep["passed"] and rep["paper"]["n"] == 40, str(rep["checks"]))
        check("재평가 리포트로 게이트 열림", check_gate(d, 3, "ladder", ("trendy_kangaroo",))[0])
        ok, why = check_gate(d, 3, "zone", ("trendy_kangaroo",))
        check("라이브 설정과 다른 전략을 평가한 리포트면 게이트 닫힘", not ok, why)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # (9) 게이트는 매일 재평가로 닫혔다가 다시 열린다 (설정에서 끈 건 열지 않음)
    import main as mainmod
    from config import Config
    d = tempfile.mkdtemp(prefix="coinbot-gate-")
    saved = (Config.DRY_RUN, Config.STATE_DIR, Config.NAKED_REQUIRE_VALIDATION)
    msgs = []
    try:
        Config.DRY_RUN, Config.STATE_DIR, Config.NAKED_REQUIRE_VALIDATION = False, d, True
        bot = mainmod.MainPipeline.__new__(mainmod.MainPipeline)

        async def send(m):
            msgs.append(m)

        bot.engine = types.SimpleNamespace(notifier=types.SimpleNamespace(send_message=send))
        bot._spawn = lambda coro, name: coro.close()
        bot.naked = types.SimpleNamespace(live_enabled=True)
        bot._naked_live_configured, bot._naked_gate_ok = True, None
        bot._apply_naked_gate()
        check("리포트 없으면 실주문 닫힘", bot.naked.live_enabled is False)
        passing = {"passed": True, "checks": {}, "dsr": 0.97, "pbo": 0.2,
                   "variant": Config.NAKED_EXIT_MODE, "patterns": list(Config.NAKED_LIVE_PATTERNS)}
        write_report(d, passing, {})
        bot._apply_naked_gate()
        check("검증 통과 리포트가 생기면 다시 열림", bot.naked.live_enabled is True)
        write_report(d, dict(passing, passed=False, checks={"x": False}), {})
        bot._apply_naked_gate()
        check("재평가에서 미통과면 다시 닫힘", bot.naked.live_enabled is False)
        bot._naked_live_configured = False
        write_report(d, passing, {})
        bot._apply_naked_gate()
        check("설정에서 끈 실주문은 게이트가 켜지 않음", bot.naked.live_enabled is False)
    finally:
        Config.DRY_RUN, Config.STATE_DIR, Config.NAKED_REQUIRE_VALIDATION = saved
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)   # 점검 결과만 보이도록 로그 억제

    print("=" * 70)
    print("코인봇 오프라인 통합 점검 (네트워크 호출 없음)")
    print("=" * 70)

    test_market_state()
    test_circuit_breaker()
    test_circuit_breaker_persistence()
    test_betti_cannot_detect_withdrawal()
    test_recorder_replayability()
    test_decide_action()
    test_news_feed()
    test_curvature()
    test_order_params()
    test_labeling_barrier_scale()
    test_shadow_mode()
    test_order_recording()
    test_order_result_parsing()
    test_risk_manager()
    test_scheduler()
    test_dry_run_execution()
    test_pbo_dsr()
    test_replay_and_rl()
    test_price_action_patterns()
    test_trade_state_machine()
    test_naked_live_execution()
    test_naked_infra()
    test_naked_review_fixes()

    print("\n" + "=" * 70)
    print(f"결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("실패 항목:")
        for f in FAIL:
            print("  -", f)
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
