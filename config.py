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
    MAX_ORDERS_PER_DAY = _env_int("MAX_ORDERS_PER_DAY", 40)
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

    # ---- 뉴스 피드 (News Agent 입력, 전부 선택) ----
    # LLM_INTERVAL_SEC(5분)마다 그대로 때리면 무료 쿼터가 금방 빠듯해진다
    # (NewsAPI 무료 플랜 100건/일). 캐시 TTL 을 그보다 길게 둔다.
    NEWS_CACHE_TTL_SEC = _env_float("NEWS_CACHE_TTL_SEC", 900.0)

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

        mode = "DRY-RUN (모의주문)" if cls.DRY_RUN else "!!! LIVE 실주문 !!!"
        logger.warning("=" * 60)
        logger.warning("실행 모드: %s", mode)
        logger.warning(
            "리스크 한도 | 1회 %s원 · 종목당 %s원 · 총 %s원 · 일손실 %s원 · 일 %d건",
            f"{cls.ORDER_SIZE_KRW:,.0f}", f"{cls.MAX_POSITION_KRW:,.0f}",
            f"{cls.MAX_TOTAL_EXPOSURE_KRW:,.0f}", f"{cls.DAILY_LOSS_LIMIT_KRW:,.0f}",
            cls.MAX_ORDERS_PER_DAY,
        )
        logger.warning("=" * 60)
