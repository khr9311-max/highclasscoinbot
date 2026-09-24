import os
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 시크릿 로딩
#   로컬  : .env (python-dotenv)
#   AWS   : SSM Parameter Store (SecureString) -- COINBOT_SECRETS=ssm 일 때
# 서버에 .env 파일을 두지 않기 위한 분기. boto3 가 없어도 로컬 동작은 그대로.
# ---------------------------------------------------------------------------

_SECRET_KEYS = (
    "UPBIT_OPEN_API_ACCESS_KEY",
    "UPBIT_OPEN_API_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GEMINI_API_KEY",
    "CRYPTOPANIC_API_KEY",
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "NEWSAPI_KEY",
)


def _load_from_ssm(path_prefix: str) -> dict:
    """SSM Parameter Store 에서 /coinbot/* 를 복호화해 읽어온다."""
    import boto3  # AWS 에서만 필요하므로 지연 임포트

    client = boto3.client("ssm")
    out, token = {}, None
    while True:
        kwargs = {"Path": path_prefix, "WithDecryption": True, "MaxResults": 10}
        if token:
            kwargs["NextToken"] = token
        resp = client.get_parameters_by_path(**kwargs)
        for p in resp.get("Parameters", []):
            out[p["Name"].rsplit("/", 1)[-1]] = p["Value"]
        token = resp.get("NextToken")
        if not token:
            break
    return out


def _load_secrets() -> dict:
    source = os.environ.get("COINBOT_SECRETS", "env").lower()

    if source == "ssm":
        prefix = os.environ.get("COINBOT_SSM_PREFIX", "/coinbot/")
        try:
            values = _load_from_ssm(prefix)
            logger.info("Secrets loaded from SSM Parameter Store (%s): %d개", prefix, len(values))
            return values
        except Exception as e:
            # 운영 중 조용히 빈 키로 돌아가면 인증 실패만 반복되므로 즉시 중단한다.
            raise RuntimeError(f"SSM 시크릿 로딩 실패 ({prefix}): {e}") from e

    from dotenv import load_dotenv

    load_dotenv()
    return {k: os.environ.get(k) for k in _SECRET_KEYS}


_SECRETS = _load_secrets()


def _secret(name: str):
    return _SECRETS.get(name) or os.environ.get(name)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _exit_mode(raw: str) -> str:
    m = raw.strip().lower()
    return "zone" if m == "auto" else m


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # ---- API 자격증명 ----
    UPBIT_ACCESS_KEY = _secret("UPBIT_OPEN_API_ACCESS_KEY")
    UPBIT_SECRET_KEY = _secret("UPBIT_OPEN_API_SECRET_KEY")
    TELEGRAM_BOT_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = _secret("TELEGRAM_CHAT_ID")
    GEMINI_API_KEY = _secret("GEMINI_API_KEY")
    CRYPTOPANIC_API_KEY = _secret("CRYPTOPANIC_API_KEY")
    NAVER_CLIENT_ID = _secret("NAVER_CLIENT_ID")
    NAVER_CLIENT_SECRET = _secret("NAVER_CLIENT_SECRET")
    NEWSAPI_KEY = _secret("NEWSAPI_KEY")

    # ---- 매매 대상 ----
    TARGET_TICKERS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL"]
    BASE_CURRENCY = "KRW"

    # ---- 안전장치 ----
    # 기본값이 True 인 것은 의도적이다. 실주문은 DRY_RUN=false 를 명시해야만 켜진다.
    DRY_RUN = _env_bool("DRY_RUN", True)

    MIN_ORDER_KRW = 5_000.0                                   # 업비트 KRW 마켓 최소 주문금액
    MAX_POSITION_KRW = _env_float("MAX_POSITION_KRW", 50_000.0)       # 종목당 최대 보유 평가액
    MAX_TOTAL_EXPOSURE_KRW = _env_float("MAX_TOTAL_EXPOSURE_KRW", 150_000.0)  # 전체 최대 노출
    ORDER_SIZE_KRW = _env_float("ORDER_SIZE_KRW", 10_000.0)           # 1회 주문금액
    DAILY_LOSS_LIMIT_KRW = _env_float("DAILY_LOSS_LIMIT_KRW", 30_000.0)  # 일일 손실 한도(초과 시 정지)
    # 일일 상한은 매수에만 건다. 매도(청산)는 세지도 막지도 않는다 -
    # 상한을 다 쓴 상태에서 급락이 오면 팔지 못하는 상황이 되기 때문.
    MAX_BUYS_PER_DAY = _env_int("MAX_BUYS_PER_DAY", 100)
    ORDER_COOLDOWN_SEC = _env_float("ORDER_COOLDOWN_SEC", 60.0)       # 동일 종목 재주문 최소 간격

    # ---- RL / 기하학 ----
    WINDOW_SIZE = 60
    GAMMA = 0.99
    LEARNING_RATE = 3e-4

    # ---- 시스템 ----
    UPDATE_INTERVAL = 1.0            # clock 틱 간격(초)
    LLM_INTERVAL_SEC = 300.0         # LLM 의사결정 주기
    TRAIN_INTERVAL_SEC = 60.0        # 온라인 학습 주기
    MAX_SLIPPAGE_RATE = 0.005        # 허용 슬리피지 (execution_engine 에서 적용)
    MARKET_DATA_STALE_SEC = 30.0     # 이 시간 넘게 시세 갱신 없으면 매매 중단
    ORDER_TIMEOUT_SEC = 60.0         # 미체결 지정가 취소까지의 시간
    STATE_DIR = os.environ.get("COINBOT_STATE_DIR", "state")

    # ---- LLM ----
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

    # ---- 최종 판정 규칙 (multi_agent.decide_action) ----
    # 진입 조건: |0.7*crypto + 0.3*news| >= MIN_ENTRY_SCORE.
    # 실측 95건(7.9시간)에 적용했을 때 진입률:
    #   0.3 -> 58% / 0.4 -> 37% / 0.5 -> 13%
    # 0.5 는 실제 급등 구간(crypto=+0.72, news=-0.20 -> 합 0.44)을 놓쳐서 0.4 로 둔다.
    MIN_ENTRY_SCORE = _env_float("MIN_ENTRY_SCORE", 0.4)

    # 메타 모델이 아직 없을 때의 대체 게이트. 진입 규칙이 이미 같은 값으로
    # 걸렀으므로 여기서는 이중 차단이 되지 않는다. 학습 전까지 더 보수적으로
    # 가려면 이 값만 올리면 된다.
    META_FALLBACK_MIN_STRENGTH = _env_float(
        "META_FALLBACK_MIN_STRENGTH", MIN_ENTRY_SCORE
    )

    # ---- 섀도 모드 ----
    # True 면 TARGET_TICKERS 전체를 판정·기록하되 주문은 TARGET_TICKERS[0]
    # (primary_ticker) 에만 낸다. 메타 모델 표본을 종목 수만큼 빨리 모으면서
    # 금전 리스크는 늘리지 않기 위함이다.
    # 비용: 크립토 에이전트 호출이 종목 수만큼 늘어난다(뉴스는 공유).
    SHADOW_MODE = _env_bool("SHADOW_MODE", True)

    # ---- 뉴스 피드 (News Agent 입력, 전부 선택) ----
    # LLM_INTERVAL_SEC(5분)마다 그대로 때리면 무료 쿼터가 금방 빠듯해진다
    # (NewsAPI 무료 플랜 100건/일). 캐시 TTL 을 그보다 길게 둔다.
    NEWS_CACHE_TTL_SEC = _env_float("NEWS_CACHE_TTL_SEC", 900.0)

    # GDELT(DOC 2.0). 키가 필요 없어 후보였지만 레이트리밋이 공격적이다.
    # 실측(2026-09-19, EC2): 10초 간격 4회 전부 429, 60초 간격 10회도 전부
    # 실패(1회는 HTTP 200 이었으나 articles 가 비어 응답 없음과 동일). 문서의
    # '5초에 1회' 보다 훨씬 엄격하게 걸린다.
    #
    # 기본 비활성인 이유는 실패해서가 아니라 '간헐적으로 성공해서' 다.
    # 15분 갱신마다 포함/누락이 갈리면 헤드라인 집합이 바뀌고, 시장과 무관한
    # 이유로 뉴스 점수가 출렁인다. 검증 중인 신호에 노이즈를 더할 이유가 없다.
    # 레이트리밋이 풀리거나 다른 IP 를 쓰게 되면 이 값만 켜면 된다.
    GDELT_ENABLED = _env_bool("GDELT_ENABLED", False)

    # ---- 전략 선택 ----
    # "naked" : 가격행동(Naked Forex) 패턴이 1차 신호. LLM 판정은 기록만 한다.
    # "llm"   : 기존 경로 (0.7*crypto + 0.3*news -> RL -> 메타 -> 주문).
    # 두 경로가 같은 종목에 동시에 주문을 내면 포지션이 서로 엉키므로 하나만 주문한다.
    STRATEGY_MODE = os.environ.get("STRATEGY_MODE", "naked").strip().lower()

    # ---- 가격행동 전략 (price_action / naked_strategy) ----
    # 책은 일봉·4시간봉을 권한다(3·13장). 1분~5분봉은 왕복비용 0.12% 가 손절폭의
    # 상당 부분을 먹는다. 판정은 1시간봉, 존은 한 단계 위인 4시간봉에서 그린다.
    NAKED_TF_MIN = _env_int("NAKED_TF_MIN", 60)
    NAKED_ZONE_TF_MIN = _env_int("NAKED_ZONE_TF_MIN", 240)
    NAKED_ZONE_BARS = _env_int("NAKED_ZONE_BARS", 200)       # 4시간봉 200개 = 약 33일
    NAKED_PATTERNS = tuple(
        p.strip() for p in os.environ.get(
            "NAKED_PATTERNS", "kangaroo,big_shadow,wammie,last_kiss,trendy_kangaroo"
        ).split(",") if p.strip()
    )
    # zone | split | ladder | three_bar
    # zone = 위에 존이 있으면 그 존에서 전량 청산, 없으면(신고가) 3봉 추적 청산.
    # (예전 이름 auto 는 zone 과 같은 규칙이라 zone 으로 읽는다)
    NAKED_EXIT_MODE = _exit_mode(os.environ.get("NAKED_EXIT_MODE", "ladder"))
    NAKED_MIN_RR = _env_float("NAKED_MIN_RR", 1.0)            # 첫 목표가 손절폭보다 가까우면 다음 존
    NAKED_ENTRY_VALID_BARS = _env_int("NAKED_ENTRY_VALID_BARS", 2)   # 매수스톱 유효 봉 수
    NAKED_MAX_HOLD_BARS = _env_int("NAKED_MAX_HOLD_BARS", 72)        # 1시간봉 72개 = 3일
    # 고정 위험 사이징: 손절에 걸리면 이 금액을 잃도록 주문금액을 정한다.
    # 주문금액은 MAX_POSITION_KRW 를 넘지 않고 MIN_ORDER_KRW 아래로 내려가지 않는다.
    NAKED_RISK_PER_TRADE_KRW = _env_float("NAKED_RISK_PER_TRADE_KRW", 1_000.0)

    # 라이브 주문. 모의(DRY_RUN) 에서는 기본으로 켜서 주문 경로까지 태우고,
    # 실주문 모드에서는 명시적으로 켜야 한다 + 검증 통과 필요(아래).
    NAKED_LIVE = _env_bool("NAKED_LIVE", DRY_RUN)
    # 라이브(주문)에 쓰는 패턴. 종이 매매·기록은 NAKED_PATTERNS 전체로 계속한다.
    #
    # 기본값 trendy_kangaroo + ladder 는 백테스트에서 '고른' 후보다
    # (2026-09-24, 180일 · 코어4 + 알트20 · 1시간봉, validate_naked.py):
    #   책 규칙 5개 전체(zone 청산) : 986건 승률 33.6% 평균 -0.064% PF 0.94  -> 비용 못 넘김
    #   trendy_kangaroo(ladder)    : 109건 승률 27.5% 평균 +0.924% PF 2.05
    #                                전반/후반 90일 +0.23%/+1.51% (두 기간 모두 24개 중 1등)
    #                                알트 +1.02% n=94 / 코어 +0.32% n=15
    # 하지만 수익이 큰 승리 몇 건에 몰려 있고(상위 5건 빼면 -0.02%), 24개 조합 중
    # 사후에 고른 것이라 DSR 0.37 · PBO 0.65 로 운과 구분되지 않는다.
    # 그래서 실주문은 검증 게이트가 막고, 지금부터 쌓이는 종이 매매(표본 외)가
    # 확인해줘야 열린다. 표본 외에서 무너지면 이 기본값을 버려야 한다.
    NAKED_LIVE_PATTERNS = tuple(
        p.strip() for p in os.environ.get("NAKED_LIVE_PATTERNS", "trendy_kangaroo").split(",")
        if p.strip()
    )
    NAKED_LIVE_TICKERS = tuple(
        t.strip() for t in os.environ.get("NAKED_LIVE_TICKERS", ",".join(TARGET_TICKERS)).split(",")
        if t.strip()
    )
    # 알트 라이브 거래. 켜면 신호가 난 알트를 그때만 웹소켓으로 구독해 주문한다
    # (ExecutionEngine.watch). 코어와 같은 리스크 한도를 탄다.
    NAKED_LIVE_ALTS = _env_bool("NAKED_LIVE_ALTS", DRY_RUN)
    # 실주문 전 검증 게이트. validate_naked.py 가 쓴 state/naked_validation.json 이
    # 통과(passed=true)이고 이 일수 안에 만들어졌어야 실주문을 낸다.
    # 봇이 하루에 한 번 리포트를 새로 쓰므로(auto_validate), 이 기한이 지났다는 건
    # 자동 검증이 며칠째 실패하고 있다는 뜻이다 - 그러면 실주문을 닫는다.
    NAKED_REQUIRE_VALIDATION = _env_bool("NAKED_REQUIRE_VALIDATION", True)
    NAKED_VALIDATION_MAX_AGE_DAYS = _env_float("NAKED_VALIDATION_MAX_AGE_DAYS", 3.0)
    NAKED_AUTO_VALIDATE = _env_bool("NAKED_AUTO_VALIDATE", True)
    NAKED_BACKTEST_DAYS = _env_int("NAKED_BACKTEST_DAYS", 180)
    NAKED_BACKTEST_REFRESH_DAYS = _env_float("NAKED_BACKTEST_REFRESH_DAYS", 30.0)

    # 필터: 뉴스 점수가 이 값 이하(최근 30분 안의 값)면 진입하지 않는다.
    NAKED_NEWS_VETO = _env_float("NAKED_NEWS_VETO", -0.5)
    # 메타 모델(가격행동 전용)은 CV AUC 가 이 이상일 때만 필터로 쓴다.
    NAKED_META_MIN_AUC = _env_float("NAKED_META_MIN_AUC", 0.55)
    NAKED_META_MIN_PROB = _env_float("NAKED_META_MIN_PROB", 0.5)

    # ---- 알트 유니버스 (섀도: 종이 매매 + 기록만) ----
    # 급등 추격용 목록이 아니다. 업비트 '주의' 지정(가격 급변·거래량 급증 등)
    # 종목은 오히려 뺀다. candle_feed.AltUniverse 참고.
    NAKED_ALTS_ENABLED = _env_bool("NAKED_ALTS_ENABLED", True)
    NAKED_ALT_TOP_N = _env_int("NAKED_ALT_TOP_N", 20)
    NAKED_ALT_MIN_TRADE_KRW = _env_float("NAKED_ALT_MIN_TRADE_KRW", 5e9)   # 24h 거래대금 50억
    NAKED_ALT_MAX_SPREAD = _env_float("NAKED_ALT_MAX_SPREAD", 0.002)
    NAKED_UNIVERSE_REFRESH_SEC = _env_float("NAKED_UNIVERSE_REFRESH_SEC", 6 * 3600.0)

    @classmethod
    def validate(cls):
        if not cls.UPBIT_ACCESS_KEY or not cls.UPBIT_SECRET_KEY:
            raise ValueError("Upbit API keys are not set (env 또는 SSM 확인).")

        if not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID:
            logger.warning("Telegram 미설정 - 알림이 비활성화됩니다.")
        if not cls.GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY 미설정 - LLM 에이전트가 중립(HOLD)으로 동작합니다.")
        if not any((cls.CRYPTOPANIC_API_KEY, cls.NAVER_CLIENT_ID, cls.NEWSAPI_KEY)):
            logger.warning("뉴스 피드 전부 미설정 - News Agent 가 중립 고정으로 동작합니다.")

        # 리스크 파라미터 정합성. 잘못 설정된 채로 실주문이 나가는 것을 막는다.
        if cls.ORDER_SIZE_KRW < cls.MIN_ORDER_KRW:
            raise ValueError(
                f"ORDER_SIZE_KRW({cls.ORDER_SIZE_KRW:,.0f})가 업비트 최소 주문금액"
                f"({cls.MIN_ORDER_KRW:,.0f})보다 작습니다."
            )
        if cls.MAX_POSITION_KRW > cls.MAX_TOTAL_EXPOSURE_KRW:
            raise ValueError("MAX_POSITION_KRW 가 MAX_TOTAL_EXPOSURE_KRW 보다 큽니다.")
        if cls.DAILY_LOSS_LIMIT_KRW <= 0:
            raise ValueError("DAILY_LOSS_LIMIT_KRW 는 0보다 커야 합니다.")

        if cls.STRATEGY_MODE not in ("naked", "llm"):
            raise ValueError(f"STRATEGY_MODE 는 naked 또는 llm 이어야 합니다: {cls.STRATEGY_MODE}")
        if cls.NAKED_EXIT_MODE not in ("zone", "split", "ladder", "three_bar"):
            raise ValueError(f"NAKED_EXIT_MODE 값 오류: {cls.NAKED_EXIT_MODE}")
        from price_action import ENTRY_PATTERNS
        unknown = (set(cls.NAKED_PATTERNS) | set(cls.NAKED_LIVE_PATTERNS)) - set(ENTRY_PATTERNS)
        if unknown:
            raise ValueError(f"NAKED_PATTERNS 에 모르는 패턴: {sorted(unknown)}")
        if not set(cls.NAKED_LIVE_PATTERNS) <= set(cls.NAKED_PATTERNS):
            raise ValueError("NAKED_LIVE_PATTERNS 는 NAKED_PATTERNS 안에 있어야 합니다 (종이 매매가 검증 근거).")
        if cls.NAKED_RISK_PER_TRADE_KRW <= 0:
            raise ValueError("NAKED_RISK_PER_TRADE_KRW 는 0보다 커야 합니다.")
        if not 60 <= cls.NAKED_ZONE_BARS <= 200:
            # 라이브 스캔은 캔들 한 번(업비트 최대 200개)만 받는다. 백테스트만 더
            # 길게 보면 라이브와 다른 존을 그리게 된다.
            raise ValueError("NAKED_ZONE_BARS 는 60~200 이어야 합니다.")
        if cls.NAKED_TF_MIN not in (1, 3, 5, 10, 15, 30, 60, 240) or \
                cls.NAKED_ZONE_TF_MIN not in (1, 3, 5, 10, 15, 30, 60, 240):
            raise ValueError("NAKED_TF_MIN / NAKED_ZONE_TF_MIN 은 업비트 분봉 단위여야 합니다.")

        mode = "DRY-RUN (모의주문)" if cls.DRY_RUN else "!!! LIVE 실주문 !!!"
        logger.warning("=" * 60)
        logger.warning("실행 모드: %s", mode)
        logger.warning(
            "전략 %s | 가격행동 라이브=%s 패턴=%s 청산=%s 알트라이브=%s",
            cls.STRATEGY_MODE, cls.NAKED_LIVE, ",".join(cls.NAKED_LIVE_PATTERNS),
            cls.NAKED_EXIT_MODE, cls.NAKED_LIVE_ALTS,
        )
        logger.warning(
            "리스크 한도 | 1회 %s원 · 종목당 %s원 · 총 %s원 · 일손실 %s원 · 일 매수 %d건(매도 무제한)",
            f"{cls.ORDER_SIZE_KRW:,.0f}", f"{cls.MAX_POSITION_KRW:,.0f}",
            f"{cls.MAX_TOTAL_EXPOSURE_KRW:,.0f}", f"{cls.DAILY_LOSS_LIMIT_KRW:,.0f}",
            cls.MAX_BUYS_PER_DAY,
        )
        logger.warning("=" * 60)
