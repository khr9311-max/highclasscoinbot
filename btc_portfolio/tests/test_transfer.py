import asyncio
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

from binance_coinm_v1.exchange.models import AccountInfo, AssetBalance, PositionInfo
from btc_portfolio.config import Config
from btc_portfolio.transfer import plan, signed_transfer
from btc_spot.gateway import HttpResponse


def account(*, permission=True, coinm="0", free="0.00412273", orders=()):
    asset = AssetBalance("BTC", float(coinm), 0, float(coinm), float(coinm))
    return {
        "spot": {"uid": 123, "canTrade": True,
                 "balances": [{"asset": "BTC", "free": free, "locked": "0"}]},
        "permissions": {"permitsUniversalTransfer": permission, "enableInternalTransfer": permission},
        "coin": AccountInfo({"BTC": asset}, []), "spot_orders": list(orders),
        "coin_orders": [], "algos": [],
    }


def test_transfer_plan_preserves_legacy_reserve_and_only_fills_coinm_gap():
    config = Config()
    preview = plan(config, account(coinm="0.0008"), Decimal("0.001"))
    assert preview["ready"]
    assert preview["amount"] == "0.0010"
    assert Decimal(preview["spot_btc_after_estimate"]) >= Decimal(preview["minimum_spot_btc_after"])
    assert not plan(config, account(free="0.003", coinm="0"), Decimal("0.001"))["ready"]
    assert "API_KEY_PERMITS_UNIVERSAL_TRANSFER_DISABLED" in plan(
        config, account(permission=False), Decimal("0.001"))["blockers"]
    assert "OPEN_ORDERS_PRESENT" in plan(
        config, account(orders=[{"symbol": "BTCUSDT"}]), Decimal("0.001"))["blockers"]


def test_signed_transfer_posts_one_fixed_btc_coins_funding_request():
    class FakeTransport:
        def __init__(self):
            self.calls = []

        async def request(self, method, url, headers, timeout, body=None):
            self.calls.append((method, url, headers, timeout, body))
            return HttpResponse(200, {}, '{"tranId":12345}')

    transport = FakeTransport()

    async def sync_time(force=False):
        assert force
        return 1000000000000

    gateway = SimpleNamespace(_api_key="test-key", _api_secret="test-secret",
                              _sync_time=sync_time, transport=transport)
    assert asyncio.run(signed_transfer(gateway, "0.0018")) == 12345
    assert len(transport.calls) == 1
    method, url, headers, _, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://api.binance.com/sapi/v1/asset/transfer"
    assert headers["X-MBX-APIKEY"] == "test-key"
    params = parse_qs(body)
    assert params["type"] == ["MAIN_CMFUTURE"]
    assert params["asset"] == ["BTC"]
    assert params["amount"] == ["0.0018"]
    assert len(params["signature"][0]) == 64
