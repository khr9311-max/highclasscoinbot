"""Explicit runtime settings; never load the legacy Upbit environment."""
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPITAL = Decimal("0.003")
DEFAULT_CREDENTIALS = ROOT / "binance_coinm_v1/.env"
LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_SPOT"


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


def credentials(path=DEFAULT_CREDENTIALS):
    # dotenv_values has no process-environment side effects. Disable expansion
    # so unrelated environment variables cannot silently change credentials.
    from dotenv import dotenv_values
    path = Path(path).resolve()
    if path == (ROOT / ".env").resolve():
        raise ValueError("Legacy root .env is not a Binance credential source")
    if not path.is_file():
        raise ValueError("Binance credential file does not exist")
    values = dotenv_values(path, interpolate=False)
    key, secret = values.get("BINANCE_API_KEY"), values.get("BINANCE_API_SECRET")
    if not key or not secret:
        raise ValueError("Binance API credentials are missing")
    return Credentials(key, secret)


def capital(value):
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError("Invalid BTC allocation") from None
    if isinstance(value, bool) or not result.is_finite() or result <= 0:
        raise ValueError("BTC allocation must be finite and positive")
    return result


def state_directory(mode):
    if mode not in {"paper", "live"}:
        raise ValueError("Unsupported mode")
    return ROOT / "btc_spot/state" / mode
