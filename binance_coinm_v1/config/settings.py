"""
설정 로딩과 검증.

- binance_coinm_v1/.env 와 프로세스 환경변수만 읽는다. 저장소 루트의 업비트
  .env 는 읽지 않는다 (두 봇이 서로의 설정을 오염시키지 않게).
- 비밀값(API 키/시크릿, 텔레그램 토큰)은 repr 에 나오지 않는다. 로그·DB·텔레그램
  에서는 storage.redact.Redactor 가 한 번 더 가린다.
- '실제 주문 허용' 판단은 여기서 하지 않는다. execution/live_gate.py 가 한다.
  여기서는 값의 형식과 V1 이 지원하는 범위만 검사한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

PACKAGE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PACKAGE_DIR / ".env"
DEFAULT_STATE_DIR = PACKAGE_DIR / "state"

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_LIVE_TRADING"

# 전략 코드(신호·청산 규칙)가 바뀌면 올린다. 검증 리포트의 설정 지문에 들어가므로
# 규칙을 바꾸면 옛 검증 결과로는 실거래 게이트가 열리지 않는다.
STRATEGY_VERSION = "trendy_kangaroo-v1.1"

TRIGGER_TYPES = ("MARK_PRICE", "CONTRACT_PRICE")
BINANCE_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")
INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                    "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200,
                    "1d": 86400}


class ConfigError(ValueError):
    pass


# 환경변수 이름 -> (필드명, 기본값). 기본값은 전부 '안전한 쪽'이다.
_ENV_MAP: Dict[str, Tuple[str, Any]] = {
    "BINANCE_API_KEY": ("api_key", ""),
    "BINANCE_API_SECRET": ("api_secret", ""),
    "BINANCE_ENV": ("binance_env", "live"),
    "EXECUTION_MODE": ("execution_mode", "paper"),
    "LIVE_TRADING_ENABLED": ("live_trading_enabled", False),
    "LIVE_CONFIRMATION": ("live_confirmation", ""),
    "SYMBOL": ("symbol", "BTCUSD_PERP"),
    "MARGIN_TYPE": ("margin_type", "ISOLATED"),
    "POSITION_MODE": ("position_mode", "ONE_WAY"),
    "LEVERAGE": ("leverage", 3),
    "RISK_PER_TRADE_PCT": ("risk_per_trade_pct", 0.5),
    "MAX_DAILY_LOSS_PCT": ("max_daily_loss_pct", 2.0),
    "MAX_POSITIONS": ("max_positions", 1),
    "ENTRY_VALID_BARS": ("entry_valid_bars", 2),
    "MAX_HOLD_BARS": ("max_hold_bars", 72),
    "EXIT_MODE": ("exit_mode", "ladder"),
    # 보호 손절(STOP_MARKET)의 트리거 가격. 비우면 MARK_PRICE (꼬리 하나로 손절이
    # 터지는 것을 줄인다). 백테스트도 이 값에 맞춰 마크가격 봉으로 손절을 판정한다.
    "MARKET_TRIGGER_TYPE": ("stop_trigger_type", ""),
    "TP_TRIGGER_TYPE": ("tp_trigger_type", ""),
    "ENTRY_TRIGGER_TYPE": ("entry_trigger_type", "CONTRACT_PRICE"),
    "STOP_PRICE_PROTECT": ("stop_price_protect", False),
    "SIGNAL_INTERVAL": ("signal_interval", "1h"),
    "ZONE_INTERVAL": ("zone_interval", "4h"),
    "ZONE_BARS": ("zone_bars", 200),
    "MIN_RR": ("min_rr", 1.0),
    "LADDER_TP_FRACTIONS": ("ladder_tp_fractions", "0.25,0.25,0.25"),
    "DIRECTIONS": ("directions", "long,short"),
    "TAKER_FEE_RATE": ("taker_fee_rate", 0.0005),
    "MAKER_FEE_RATE": ("maker_fee_rate", 0.0002),
    "SLIPPAGE_BPS": ("slippage_bps", 3.0),
    "STOP_SLIPPAGE_BPS": ("stop_slippage_bps", 5.0),
    "MAX_EXPOSURE_MULTIPLE": ("max_exposure_multiple", 3.0),
    "LIQ_GUARD_MIN_RATIO": ("liq_guard_min_ratio", 2.0),
    "ORPHAN_POSITION_POLICY": ("orphan_position_policy", "protect"),
    "ORPHAN_STOP_PCT": ("orphan_stop_pct", 3.0),
    "FOREIGN_ORDER_POLICY": ("foreign_order_policy", "block"),
    "PAPER_START_EQUITY_BTC": ("paper_start_equity_btc", 0.007),
    "STATE_DIR": ("state_dir", ""),
    "USD_KRW_SOURCE": ("usd_krw_source", "fixed"),
    "USD_KRW_RATE": ("usd_krw_rate", 1390.0),
    "TELEGRAM_BOT_TOKEN": ("telegram_token", ""),
    "TELEGRAM_CHAT_ID": ("telegram_chat_id", ""),
    "VALIDATION_MIN_TRADES": ("validation_min_trades", 100),
    "VALIDATION_MIN_DSR": ("validation_min_dsr", 0.95),
    "VALIDATION_MAX_PBO": ("validation_max_pbo", 0.5),
    "VALIDATION_MAX_DD_PCT": ("validation_max_dd_pct", 30.0),
    "VALIDATION_MIN_PAPER_TRADES": ("validation_min_paper_trades", 30),
    "VALIDATION_MAX_AGE_DAYS": ("validation_max_age_days", 3.0),
    "RECONCILE_INTERVAL_SEC": ("reconcile_interval_sec", 300.0),
    "MARKET_WS_STALE_SEC": ("market_ws_stale_sec", 30.0),
    "RECV_WINDOW_MS": ("recv_window_ms", 5000),
    "ENTRY_MAX_ATTEMPTS": ("entry_max_attempts", 2),
    "RESTART_PENDING_GRACE_SEC": ("restart_pending_grace_sec", 120.0),
    "PAPER_READ_ACCOUNT": ("paper_read_account", False),
}

_SECRET_FIELDS = ("api_key", "api_secret", "telegram_token")


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    raise ConfigError(f"불리언 값이 아님: {v!r}")


def _to_float(name: str, v: Any) -> float:
    try:
        value = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: 숫자가 아님 ({v!r})") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name}: 유한한 숫자가 필요함")
    return value


def _to_int(name: str, v: Any) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: 정수가 아님 ({v!r})") from None
    if not math.isfinite(f) or f != int(f):
        raise ConfigError(f"{name}: 정수가 아님 ({v!r})")
    return int(f)


@dataclass(frozen=True)
class Settings:
    # ---- 비밀값 (repr 제외) ----
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)

    # ---- 실행 환경 ----
    binance_env: str = "live"
    execution_mode: str = "paper"
    live_trading_enabled: bool = False
    live_confirmation: str = field(default="", repr=False)

    # ---- 계약·계정 ----
    symbol: str = "BTCUSD_PERP"
    margin_type: str = "ISOLATED"
    position_mode: str = "ONE_WAY"
    leverage: int = 3

    # ---- 위험 ----
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_positions: int = 1
    max_exposure_multiple: float = 3.0
    liq_guard_min_ratio: float = 2.0

    # ---- 전략 ----
    entry_valid_bars: int = 2
    max_hold_bars: int = 72
    exit_mode: str = "ladder"
    signal_interval: str = "1h"
    zone_interval: str = "4h"
    zone_bars: int = 200
    min_rr: float = 1.0
    ladder_tp_fractions: Tuple[float, float, float] = (0.25, 0.25, 0.25)
    directions: Tuple[int, ...] = (1, -1)

    # ---- 주문 방식 ----
    stop_trigger_type: str = "MARK_PRICE"
    tp_trigger_type: str = "CONTRACT_PRICE"
    entry_trigger_type: str = "CONTRACT_PRICE"
    stop_price_protect: bool = False

    # ---- 비용 가정 (실거래는 commissionRate 로 덮어쓴다) ----
    taker_fee_rate: float = 0.0005
    maker_fee_rate: float = 0.0002
    slippage_bps: float = 3.0
    stop_slippage_bps: float = 5.0

    # ---- 복구 정책 ----
    orphan_position_policy: str = "protect"
    orphan_stop_pct: float = 3.0
    foreign_order_policy: str = "block"
    restart_pending_grace_sec: float = 120.0
    entry_max_attempts: int = 2

    # ---- 종이 매매 ----
    paper_start_equity_btc: float = 0.007
    paper_read_account: bool = False

    # ---- 저장·표시 ----
    state_dir: str = str(DEFAULT_STATE_DIR)
    usd_krw_source: str = "fixed"
    usd_krw_rate: float = 1390.0

    # ---- 검증 게이트 ----
    validation_min_trades: int = 100
    validation_min_dsr: float = 0.95
    validation_max_pbo: float = 0.5
    validation_max_dd_pct: float = 30.0
    validation_min_paper_trades: int = 30
    validation_max_age_days: float = 3.0

    # ---- 운영 ----
    reconcile_interval_sec: float = 300.0
    market_ws_stale_sec: float = 30.0
    recv_window_ms: int = 5000

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env_file: Optional[os.PathLike] = None,
             environ: Optional[Mapping[str, str]] = None) -> "Settings":
        """binance_coinm_v1/.env -> 환경변수 순으로 덮어쓴다 (환경변수가 이긴다)."""
        merged: Dict[str, str] = {}
        path = Path(env_file) if env_file else DEFAULT_ENV_FILE
        if path.exists():
            from dotenv import dotenv_values
            for k, v in dotenv_values(path).items():
                if k in _ENV_MAP and v is not None:
                    merged[k] = v
        env = os.environ if environ is None else environ
        for k in _ENV_MAP:
            if k in env and env[k] is not None:
                merged[k] = env[k]
        return cls.from_mapping(merged)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "Settings":
        kw: Dict[str, Any] = {}
        for env_name, (fname, default) in _ENV_MAP.items():
            raw = mapping.get(env_name, None)
            if raw is None or (isinstance(raw, str) and raw.strip() == "" and fname not in (
                    "stop_trigger_type", "tp_trigger_type")):
                continue
            kw[fname] = raw
        return cls.build(**kw)

    @classmethod
    def build(cls, **kw: Any) -> "Settings":
        """문자열·숫자가 섞인 입력을 정규화하고 검증한다 (테스트는 이걸 쓴다)."""
        d: Dict[str, Any] = {}
        defaults = cls()
        for k, v in kw.items():
            if not hasattr(defaults, k):
                raise ConfigError(f"알 수 없는 설정: {k}")
            d[k] = v

        def s(name, upper=False):
            if name in d:
                val = str(d[name]).strip()
                d[name] = val.upper() if upper else val

        for name in ("api_key", "api_secret", "telegram_token", "telegram_chat_id",
                     "live_confirmation", "state_dir"):
            s(name)
        for name in ("binance_env", "execution_mode", "exit_mode", "signal_interval",
                     "zone_interval", "orphan_position_policy", "foreign_order_policy",
                     "usd_krw_source"):
            if name in d:
                d[name] = str(d[name]).strip().lower()
        for name in ("symbol", "margin_type", "position_mode", "stop_trigger_type",
                     "tp_trigger_type", "entry_trigger_type"):
            s(name, upper=True)
        for name in ("live_trading_enabled", "stop_price_protect", "paper_read_account"):
            if name in d:
                d[name] = _to_bool(d[name])
        for name in ("leverage", "max_positions", "entry_valid_bars", "max_hold_bars",
                     "zone_bars", "validation_min_trades", "validation_min_paper_trades",
                     "recv_window_ms", "entry_max_attempts"):
            if name in d:
                d[name] = _to_int(name, d[name])
        for name in ("risk_per_trade_pct", "max_daily_loss_pct", "min_rr", "taker_fee_rate",
                     "maker_fee_rate", "slippage_bps", "stop_slippage_bps",
                     "max_exposure_multiple", "liq_guard_min_ratio", "orphan_stop_pct",
                     "paper_start_equity_btc", "usd_krw_rate", "validation_min_dsr",
                     "validation_max_pbo", "validation_max_dd_pct",
                     "validation_max_age_days", "reconcile_interval_sec",
                     "market_ws_stale_sec", "restart_pending_grace_sec"):
            if name in d:
                d[name] = _to_float(name, d[name])

        if "ladder_tp_fractions" in d:
            v = d["ladder_tp_fractions"]
            parts = v.split(",") if isinstance(v, str) else list(v)
            try:
                fr = tuple(float(x) for x in parts)
            except ValueError:
                raise ConfigError(f"LADDER_TP_FRACTIONS 형식 오류: {v!r}") from None
            d["ladder_tp_fractions"] = fr
        if "directions" in d:
            v = d["directions"]
            parts = v.split(",") if isinstance(v, str) else list(v)
            dirs: List[int] = []
            for p in parts:
                p = str(p).strip().lower()
                if p in ("long", "1", "+1"):
                    dirs.append(1)
                elif p in ("short", "-1"):
                    dirs.append(-1)
                elif p:
                    raise ConfigError(f"DIRECTIONS 값 오류: {p!r} (long,short)")
            d["directions"] = tuple(sorted(set(dirs), reverse=True))
        # 트리거 가격: 비우면 기본값
        if d.get("stop_trigger_type", None) == "":
            d["stop_trigger_type"] = "MARK_PRICE"
        if d.get("tp_trigger_type", None) == "":
            d["tp_trigger_type"] = "CONTRACT_PRICE"
        if d.get("state_dir", None) == "":
            d["state_dir"] = str(DEFAULT_STATE_DIR)

        obj = cls(**d)
        obj.validate()
        return obj

    # ------------------------------------------------------------------
    def validate(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            values = value if isinstance(value, tuple) else (value,)
            if any(isinstance(x, float) and not math.isfinite(x) for x in values):
                raise ConfigError(f"{name}: 유한한 숫자가 필요함")
        if self.binance_env not in ("live", "testnet"):
            raise ConfigError(f"BINANCE_ENV 는 live/testnet: {self.binance_env!r}")
        if self.execution_mode not in ("paper", "testnet", "live"):
            raise ConfigError(f"EXECUTION_MODE 는 paper/testnet/live: {self.execution_mode!r}")
        if self.execution_mode == "testnet" and self.binance_env != "testnet":
            # 이 조합은 '테스트넷 주문' 을 실계정으로 보내는 사고가 된다.
            raise ConfigError("EXECUTION_MODE=testnet 은 BINANCE_ENV=testnet 에서만 허용")
        if not self.symbol or not self.symbol.replace("_", "").isalnum():
            raise ConfigError(f"SYMBOL 형식 오류: {self.symbol!r}")
        if self.margin_type != "ISOLATED":
            raise ConfigError("V1 은 MARGIN_TYPE=ISOLATED 만 지원 (청산가 계산이 격리 기준)")
        if self.position_mode != "ONE_WAY":
            raise ConfigError("V1 은 POSITION_MODE=ONE_WAY 만 지원 (reduceOnly 청산 전제)")
        if not 1 <= self.leverage <= 20:
            raise ConfigError("LEVERAGE 는 1~20 (V1 안전 상한)")
        if not 0 < self.risk_per_trade_pct <= 2.0:
            raise ConfigError("RISK_PER_TRADE_PCT 는 0 초과 2.0 이하 (오타 방지 상한)")
        if not 0 < self.max_daily_loss_pct <= 10.0:
            raise ConfigError("MAX_DAILY_LOSS_PCT 는 0 초과 10 이하")
        if self.max_positions != 1:
            raise ConfigError("V1 은 MAX_POSITIONS=1 만 지원")
        if self.entry_valid_bars < 1 or self.max_hold_bars < 1:
            raise ConfigError("ENTRY_VALID_BARS / MAX_HOLD_BARS 는 1 이상")
        if self.exit_mode != "ladder":
            raise ConfigError("V1 은 EXIT_MODE=ladder 만 지원")
        for name, v in (("MARKET_TRIGGER_TYPE", self.stop_trigger_type),
                        ("TP_TRIGGER_TYPE", self.tp_trigger_type),
                        ("ENTRY_TRIGGER_TYPE", self.entry_trigger_type)):
            if v not in TRIGGER_TYPES:
                raise ConfigError(f"{name} 는 {TRIGGER_TYPES} 중 하나: {v!r}")
        if self.signal_interval not in BINANCE_INTERVALS or self.zone_interval not in BINANCE_INTERVALS:
            raise ConfigError("SIGNAL_INTERVAL / ZONE_INTERVAL 은 바이낸스 봉 단위여야 함")
        if INTERVAL_SECONDS[self.zone_interval] <= INTERVAL_SECONDS[self.signal_interval]:
            raise ConfigError("ZONE_INTERVAL 은 SIGNAL_INTERVAL 보다 커야 함 (상위 시간봉 존)")
        if not 60 <= self.zone_bars <= 1500:
            raise ConfigError("ZONE_BARS 는 60~1500")
        if self.min_rr < 0:
            raise ConfigError("MIN_RR 는 0 이상")
        fr = self.ladder_tp_fractions
        if len(fr) != 3 or any(x < 0 or x > 1 for x in fr) or sum(fr) > 1.0 + 1e-9:
            raise ConfigError("LADDER_TP_FRACTIONS 는 0~1 사이 3개, 합 1 이하 (TP1,TP2,TP3)")
        if not self.directions:
            raise ConfigError("DIRECTIONS 가 비었음")
        if not (0 <= self.taker_fee_rate < 0.01 and 0 <= self.maker_fee_rate < 0.01):
            raise ConfigError("수수료율 범위 오류")
        if self.slippage_bps < 0 or self.stop_slippage_bps < 0:
            raise ConfigError("슬리피지는 0 이상")
        if self.max_exposure_multiple <= 0:
            raise ConfigError("MAX_EXPOSURE_MULTIPLE 은 0 초과")
        if self.liq_guard_min_ratio < 1.0:
            raise ConfigError("LIQ_GUARD_MIN_RATIO 는 1 이상 (청산가가 손절보다 가까우면 안 됨)")
        if self.orphan_position_policy not in ("protect", "close"):
            raise ConfigError("ORPHAN_POSITION_POLICY 는 protect/close")
        if not 0.1 <= self.orphan_stop_pct <= 20:
            raise ConfigError("ORPHAN_STOP_PCT 는 0.1~20")
        if self.foreign_order_policy not in ("block", "ignore"):
            raise ConfigError("FOREIGN_ORDER_POLICY 는 block/ignore")
        if self.paper_start_equity_btc <= 0:
            raise ConfigError("PAPER_START_EQUITY_BTC 는 0 초과")
        if self.usd_krw_source not in ("fixed", "upbit_usdt"):
            raise ConfigError("USD_KRW_SOURCE 는 fixed/upbit_usdt")
        if self.usd_krw_rate <= 0:
            raise ConfigError("USD_KRW_RATE 는 0 초과")
        if not 1000 <= self.recv_window_ms <= 60000:
            raise ConfigError("RECV_WINDOW_MS 는 1000~60000")
        if not 1 <= self.entry_max_attempts <= 3:
            raise ConfigError("ENTRY_MAX_ATTEMPTS 는 1~3")
        if self.validation_min_trades < 1 or self.validation_min_paper_trades < 1:
            raise ConfigError("검증 거래 표본 수는 1 이상")
        if not 0 <= self.validation_min_dsr <= 1 or not 0 <= self.validation_max_pbo <= 1:
            raise ConfigError("DSR/PBO 검증 기준은 0~1")
        if not 0 < self.validation_max_dd_pct <= 100:
            raise ConfigError("검증 최대낙폭은 0 초과 100 이하")
        if min(self.validation_max_age_days, self.reconcile_interval_sec, self.market_ws_stale_sec) <= 0:
            raise ConfigError("검증 유효기간·대사 간격·시세 제한은 0 초과")
        if self.restart_pending_grace_sec < 0:
            raise ConfigError("재시작 유예 시간은 0 이상")

    # ------------------------------------------------------------------
    @property
    def risk_fraction(self) -> float:
        return self.risk_per_trade_pct / 100.0

    @property
    def signal_period_sec(self) -> int:
        return INTERVAL_SECONDS[self.signal_interval]

    @property
    def zone_period_sec(self) -> int:
        return INTERVAL_SECONDS[self.zone_interval]

    @property
    def has_api_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def db_path(self) -> str:
        return str(Path(self.state_dir) / "coinm_v1.sqlite3")

    def secrets(self) -> List[str]:
        """로그·DB·알림에서 가려야 할 값들."""
        return [getattr(self, f) for f in _SECRET_FIELDS if getattr(self, f)]

    def public_dict(self) -> Dict[str, Any]:
        """비밀값을 뺀 설정 (리포트·로그용)."""
        out = {}
        for k in self.__dataclass_fields__:
            if k in _SECRET_FIELDS or k in ("telegram_chat_id", "live_confirmation"):
                continue
            v = getattr(self, k)
            out[k] = list(v) if isinstance(v, tuple) else v
        out["has_api_keys"] = self.has_api_keys
        out["telegram_enabled"] = self.telegram_enabled
        out["live_confirmation_set"] = bool(self.live_confirmation)
        return out

    def strategy_params(self) -> Dict[str, Any]:
        """백테스트 결과를 바꾸는 설정 전부. 하나라도 바뀌면 검증을 다시 해야 한다."""
        return {
            "strategy_version": STRATEGY_VERSION,
            "symbol": self.symbol,
            "signal_interval": self.signal_interval,
            "zone_interval": self.zone_interval,
            "zone_bars": self.zone_bars,
            "min_rr": self.min_rr,
            "entry_valid_bars": self.entry_valid_bars,
            "max_hold_bars": self.max_hold_bars,
            "exit_mode": self.exit_mode,
            "ladder_tp_fractions": list(self.ladder_tp_fractions),
            "directions": list(self.directions),
            "stop_trigger_type": self.stop_trigger_type,
            "tp_trigger_type": self.tp_trigger_type,
            "entry_trigger_type": self.entry_trigger_type,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_exposure_multiple": self.max_exposure_multiple,
            "liq_guard_min_ratio": self.liq_guard_min_ratio,
            "stop_price_protect": self.stop_price_protect,
            "leverage": self.leverage,
            "margin_type": self.margin_type,
            "taker_fee_rate": self.taker_fee_rate,
            "maker_fee_rate": self.maker_fee_rate,
            "slippage_bps": self.slippage_bps,
            "stop_slippage_bps": self.stop_slippage_bps,
        }

    def validation_policy(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__ if k.startswith("validation_")}

    def fingerprint(self, contract_essentials: Optional[Mapping[str, Any]] = None) -> str:
        payload = {"strategy": self.strategy_params(),
                   "contract": dict(contract_essentials or {})}
        raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]


def iter_env_names() -> Iterable[str]:
    return _ENV_MAP.keys()
