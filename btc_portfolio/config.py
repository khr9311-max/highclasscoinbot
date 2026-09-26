from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from btc_spot.store import number

ROOT = Path(__file__).resolve().parents[1]
ALTS = ("ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC")


@dataclass(frozen=True)
class Config:
    spot_btc: str = "0.0012"
    coinm_btc: str = "0.0018"
    risk_fraction: str = "0.005"
    daily_loss_fraction: str = "0.02"
    alt_stop_fraction: str = "0.05"
    alt_max_fraction: str = "0.25"
    coinm_max_exposure: str = "1.25"
    max_leverage: int = 3
    symbols: tuple = ALTS
    strategy_mode: str = "swing"
    intraday_cooldown_seconds: int = 900
    intraday_max_hold_seconds: int = 14400
    spot_fraction: str = "0.5"
    leverage_mode: str = "vol"
    leverage_base: str = "2.0"
    leverage_min: str = "1.0"
    leverage_max: str = "3.0"
    vol_ref: str = "0.0113579"
    stop_fraction: str = "0.12"
    rebalance_mode: str = "alert"
    kill_fraction: str = "0.25"
    timing_prediction_path: str = ""
    timing_max_wait_seconds: int = 14400

    def __post_init__(self):
        if self.strategy_mode not in {"swing", "intraday", "aggressive"}:
            raise ValueError("Unsupported portfolio strategy mode")
        if self.strategy_mode == "aggressive":
            if not 0 < number(self.spot_fraction) <= 1:
                raise ValueError("Invalid aggressive spot fraction")
            if self.leverage_mode not in {"vol", "fixed"} or self.rebalance_mode not in {"alert", "auto"}:
                raise ValueError("Invalid aggressive leverage or rebalance mode")
            if not (0 < number(self.leverage_min) <= number(self.leverage_base) <= number(self.leverage_max) <= 3):
                raise ValueError("Aggressive leverage must stay within 1..3")
            if not (0 < number(self.vol_ref) < 1 and 0 < number(self.stop_fraction) < 1
                    and 0 < number(self.kill_fraction) < 1):
                raise ValueError("Invalid aggressive volatility, stop or kill threshold")
        if self.timing_prediction_path and (self.strategy_mode != "aggressive"
                                            or not Path(self.timing_prediction_path).is_absolute()):
            raise ValueError("Execution timing needs aggressive mode and an absolute prediction path")
        if type(self.timing_max_wait_seconds) is not int or not 0 <= self.timing_max_wait_seconds <= 14400:
            raise ValueError("Execution timing wait must be 0..14400 seconds")
        if (type(self.intraday_cooldown_seconds) is not int or not 300 <= self.intraday_cooldown_seconds <= 3600
                or type(self.intraday_max_hold_seconds) is not int or not 1800 <= self.intraday_max_hold_seconds <= 86400):
            raise ValueError("Invalid intraday holding or cooldown interval")
        required_positive = (("spot_btc",) if self.strategy_mode == "aggressive" else
                             ("spot_btc", "coinm_btc", "risk_fraction", "daily_loss_fraction",
                              "alt_stop_fraction", "alt_max_fraction", "coinm_max_exposure"))
        for name in required_positive:
            if number(getattr(self, name)) <= 0:
                raise ValueError("Configuration amounts must be positive")
        if self.strategy_mode == "aggressive" and number(self.coinm_btc) < 0:
            raise ValueError("COIN-M allocation cannot be negative")
        if self.strategy_mode != "aggressive" and not Decimal(".0001") <= number(self.risk_fraction) <= Decimal(".005"):
            raise ValueError("Per-entry BTC risk must not exceed 0.5%")
        if self.strategy_mode != "aggressive" and number(self.daily_loss_fraction) > Decimal(".02"):
            raise ValueError("Daily new-entry loss limit must not exceed 2%")
        if self.strategy_mode != "aggressive" and not Decimal(".01") <= number(self.alt_stop_fraction) <= Decimal(".10"):
            raise ValueError("Invalid alt stop distance")
        if self.strategy_mode != "aggressive" and (number(self.alt_max_fraction) > Decimal(".5") or number(self.coinm_max_exposure) > 2):
            raise ValueError("Exposure limit exceeded")
        if type(self.max_leverage) is not int or not 1 <= self.max_leverage <= 3:
            raise ValueError("Leverage must be 1..3")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols) or set(self.symbols)-set(ALTS):
            raise ValueError("Unsupported BTC-quoted spot universe")
        if self.strategy_mode == "aggressive" and set(self.symbols) != set(ALTS):
            raise ValueError("Aggressive replay requires all four BTC-quoted alt symbols")

    @property
    def total(self):
        return number(self.spot_btc) + number(self.coinm_btc)

    def identity(self):
        digest = hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode())
        for name in ("signals.py", "engine.py", "store.py", "venues.py", "spot_gateway.py", "filters.py", "runtime.py"):
            digest.update((Path(__file__).parent/name).read_text(encoding="utf-8").encode())
        if self.strategy_mode == "intraday":
            digest.update((Path(__file__).parent/"intraday.py").read_text(encoding="utf-8").encode())
        if self.strategy_mode == "aggressive":
            digest.update((Path(__file__).parent/"aggressive.py").read_text(encoding="utf-8").encode())
            digest.update((Path(__file__).parent/"transfer.py").read_text(encoding="utf-8").encode())
            if self.timing_prediction_path:
                digest.update((Path(__file__).parent/"timing.py").read_text(encoding="utf-8").encode())
        for name in ("btc_spot/engine.py", "btc_lab/market_fit.py", "binance_coinm_v1/exchange/binance_gateway.py",
                     "binance_coinm_v1/exchange/rest_client.py", "binance_coinm_v1/exchange/contract.py"):
            digest.update((ROOT/name).read_text(encoding="utf-8").encode())
        return digest.hexdigest()


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    if "symbols" in data:
        data["symbols"] = tuple(data["symbols"])
    return Config(**data)
