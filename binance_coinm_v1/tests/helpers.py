"""테스트 공용 생성기."""

import json
from decimal import Decimal
from pathlib import Path

from binance_coinm_v1.config import LIVE_CONFIRMATION_PHRASE, Settings
from binance_coinm_v1.exchange.contract import resolve_contract

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_spec(symbol="BTCUSD_PERP"):
    with open(FIXTURES / "exchange_info_coinm.json", encoding="utf-8") as f:
        xi = json.load(f)
    base = symbol.split("USD")[0]
    return resolve_contract(xi, symbol, margin_asset=base, base_asset=base)


def live_settings(**over):
    kw = dict(api_key="k" * 64, api_secret="s" * 64, binance_env="live", execution_mode="live",
              live_trading_enabled=True, live_confirmation=LIVE_CONFIRMATION_PHRASE)
    kw.update(over)
    return Settings.build(**kw)
