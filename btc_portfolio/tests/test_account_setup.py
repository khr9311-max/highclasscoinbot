from decimal import Decimal

import pytest

from binance_coinm_v1.exchange.models import AccountInfo, AssetBalance, PositionInfo
from btc_portfolio.account_setup import preview, setup_guard


def account(*, margin="cross", leverage=20, quantity="0", orders=()):
    position = PositionInfo("BTCUSD_PERP", Decimal(quantity), 0, 0, 0, 0,
                            leverage, margin, 0)
    return {"position": position, "coin": AccountInfo({"BTC": AssetBalance("BTC", 0, 0, 0, 0)},
            [position]), "coin_orders": list(orders), "algos": [], "hedge_mode": False,
            "permissions": {"enableFutures": True}}


def test_setup_preview_requires_flat_account_and_shows_existing_settings():
    result = preview(account(), 3)
    assert result["ready"]
    assert (result["margin_type_now"], result["leverage_now"]) == ("cross", 20)
    assert "COINM_POSITION_PRESENT" in preview(account(quantity="1"), 3)["blockers"]
    assert "COINM_OPEN_ORDERS" in preview(account(orders=[object()]), 3)["blockers"]


def test_setup_guard_allows_only_narrow_btcusd_perp_mutations():
    setup_guard("POST", "/dapi/v1/marginType", {"symbol": "BTCUSD_PERP", "marginType": "ISOLATED"}, 3)
    setup_guard("POST", "/dapi/v1/leverage", {"symbol": "BTCUSD_PERP", "leverage": 3}, 3)
    for path, params in (("/dapi/v1/order", {"symbol": "BTCUSD_PERP"}),
                         ("/dapi/v1/leverage", {"symbol": "BTCUSD_PERP", "leverage": 20}),
                         ("/dapi/v1/marginType", {"symbol": "ETHUSD_PERP", "marginType": "ISOLATED"})):
        with pytest.raises(ValueError):
            setup_guard("POST", path, params, 3)
