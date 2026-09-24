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

    def __post_init__(self):
        for name in ("spot_btc", "coinm_btc", "risk_fraction", "daily_loss_fraction",
                     "alt_stop_fraction", "alt_max_fraction", "coinm_max_exposure"):
            if number(getattr(self, name)) <= 0:
                raise ValueError("Configuration amounts must be positive")
        if not Decimal(".0001") <= number(self.risk_fraction) <= Decimal(".005"):
            raise ValueError("Per-entry BTC risk must not exceed 0.5%")
        if number(self.daily_loss_fraction) > Decimal(".02"):
            raise ValueError("Daily new-entry loss limit must not exceed 2%")
        if not Decimal(".01") <= number(self.alt_stop_fraction) <= Decimal(".10"):
            raise ValueError("Invalid alt stop distance")
        if number(self.alt_max_fraction) > Decimal(".5") or number(self.coinm_max_exposure) > 2:
            raise ValueError("Exposure limit exceeded")
        if type(self.max_leverage) is not int or not 1 <= self.max_leverage <= 3:
            raise ValueError("Leverage must be 1..3")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols) or set(self.symbols)-set(ALTS):
            raise ValueError("Unsupported BTC-quoted spot universe")

    @property
    def total(self):
        return number(self.spot_btc) + number(self.coinm_btc)

    def identity(self):
        digest = hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode())
        for name in ("signals.py", "engine.py", "store.py", "venues.py", "spot_gateway.py", "filters.py", "runtime.py"):
            digest.update((Path(__file__).parent/name).read_text(encoding="utf-8").encode())
        for name in ("btc_spot/engine.py", "btc_lab/market_fit.py", "binance_coinm_v1/exchange/binance_gateway.py",
                     "binance_coinm_v1/exchange/rest_client.py", "binance_coinm_v1/exchange/contract.py"):
            digest.update((ROOT/name).read_text(encoding="utf-8").encode())
        return digest.hexdigest()


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    if "symbols" in data:
        data["symbols"] = tuple(data["symbols"])
    return Config(**data)
