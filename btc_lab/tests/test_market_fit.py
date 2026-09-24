from decimal import Decimal as D
import json
from unittest.mock import Mock

import pytest

from btc_lab.market_fit import (PublicClient, _comparison, ceil_step, floor_step, market_rules,
                                minimum_market_quantity, scan, size_spot_order)


def filters(*, minimum="0.00001", step="0.00001", maximum="9000", notional="5", market=None):
    rows = [{"filterType": "LOT_SIZE", "minQty": minimum, "maxQty": maximum, "stepSize": step},
            {"filterType": "NOTIONAL", "minNotional": notional, "applyMinToMarket": True,
             "maxNotional": "9000000", "applyMaxToMarket": False, "avgPriceMins": 5}]
    if market:
        rows.append({"filterType": "MARKET_LOT_SIZE", **market})
    return rows


def order(**changes):
    values = dict(btc_balance="0.003", quote_balance="0", target_btc_fraction="0",
                  bid="80000", ask="80000", reference_price="80000",
                  filters=filters(), fee_rate=".001", slippage_bps="0")
    values.update(changes)
    return size_spot_order(**values)


def test_zero_market_fields_fall_back_and_enabled_max_still_applies():
    rule = market_rules(filters(market={"minQty": "0", "maxQty": ".1", "stepSize": "0"}))
    assert rule.min_qty == D(".00001")
    assert rule.step == D(".00001")
    assert rule.max_qty == D(".1")


def test_exact_intersection_of_unequal_non_power_of_ten_steps():
    rule = market_rules(filters(minimum=".002", step=".002", market={"minQty": ".003", "maxQty": "1", "stepSize": ".003"}))
    assert rule.step == D(".006")
    assert floor_step(".011", rule.step) == D(".006")
    assert ceil_step(".011", rule.step) == D(".012")


def test_minimum_is_ceiled_using_notional_reference_not_quantity_precision():
    rule = market_rules(filters())
    assert minimum_market_quantity(rule, "80000") == D(".00007")
    assert minimum_market_quantity(rule, "100000") == D(".00005")


def test_small_target_is_skipped_instead_of_upsized():
    answer = order(target_btc_fraction=".99")
    assert answer["status"] == "SKIP"
    assert answer["reason"] == "below_minimum_notional_no_upsize"
    assert answer["quantity"] == "0"
    assert D(answer["btc_after"]) == D(".003")


def test_buy_received_fee_is_base_and_spending_cannot_borrow():
    answer = order(btc_balance="0", quote_balance="100", target_btc_fraction="1",
                   bid="100", ask="100", reference_price="100",
                   filters=filters(minimum=".1", step=".1", notional="1"), fee_rate=".01")
    assert answer["status"] == "READY"
    assert D(answer["quantity"]) == D("1")
    assert D(answer["btc_after"]) == D(".99")
    assert D(answer["fee_btc"]) == D(".01")
    assert D(answer["fee_quote"]) == 0
    assert D(answer["fee_quote_equivalent"]) == 1
    assert D(answer["quote_after"]) == 0


def test_optional_quote_fee_reserves_commission_before_flooring_buy():
    answer = order(btc_balance="0", quote_balance="100", target_btc_fraction="1",
                   bid="100", ask="100", reference_price="100",
                   filters=filters(minimum=".1", step=".1", notional="1"), fee_rate=".01", fee_asset_mode="quote")
    assert D(answer["quantity"]) == D(".9")
    assert D(answer["fee_quote"]) == D(".9")
    assert D(answer["quote_after"]) == D("9.1")
    assert D(answer["fee_btc"]) == 0


def test_sell_deducts_quote_fee_and_never_sells_more_than_free_btc():
    answer = order(btc_balance=".003004")
    assert D(answer["quantity"]) == D(".003")
    assert D(answer["btc_after"]) == D(".000004")
    assert D(answer["quote_after"]) == D("239.760")
    assert D(answer["fee_quote"]) == D(".240")


@pytest.mark.parametrize("flag", [False, "false"])
def test_minimum_notional_market_flag_is_respected(flag):
    rule_filters = filters()
    rule_filters[-1]["applyMinToMarket"] = flag
    answer = order(target_btc_fraction=".99", filters=rule_filters)
    assert answer["status"] == "READY"
    assert D(answer["quantity"]) < D(".00007")


def test_min_notional_apply_to_market_and_usdm_notional_field():
    base = filters()[:1]
    spot = base + [{"filterType": "MIN_NOTIONAL", "minNotional": "100", "applyToMarket": False, "avgPriceMins": 5}]
    assert market_rules(spot).min_notional == 0
    usd = base + [{"filterType": "MIN_NOTIONAL", "notional": "50"}]
    assert market_rules(usd).min_notional == 50


@pytest.mark.parametrize("maximum_enabled", [False, True])
def test_maximum_notional_market_flag_caps_quantity_only_when_enabled(maximum_enabled):
    rule_filters = filters()
    rule_filters[-1].update(maxNotional="50", applyMaxToMarket=maximum_enabled)
    answer = order(filters=rule_filters)
    if maximum_enabled:
        assert D(answer["quantity"]) * 80000 <= 50
    else:
        assert D(answer["quantity"]) == D(".003")


def test_market_max_qty_caps_order():
    answer = order(filters=filters(market={"minQty": "0", "maxQty": ".0002", "stepSize": "0"}))
    assert D(answer["quantity"]) == D(".0002")


def test_reference_price_can_reject_trade_accepted_at_book_price():
    answer = order(btc_balance=".00006", reference_price="80000", bid="100000", ask="100000")
    assert answer["status"] == "SKIP"
    accepted = order(btc_balance=".00006", reference_price="100000", bid="100000", ask="100000")
    assert accepted["status"] == "READY"


def test_sizing_without_reference_is_explicitly_indicative():
    answer = order(reference_price=None, slippage_bps="3")
    assert answer["status"] == "READY"
    assert not answer["reference_price_confirmed"]


@pytest.mark.parametrize("changes", [{"btc_balance": "NaN"}, {"quote_balance": "Infinity"},
    {"btc_balance": "-.1"}, {"target_btc_fraction": "1.1"}, {"bid": "80001"},
    {"fee_rate": "1"}, {"slippage_bps": "10000"}, {"fee_asset_mode": "unknown"}])
def test_invalid_inputs_fail(changes):
    with pytest.raises(ValueError):
        order(**changes)


def test_post_fee_allocation_moves_toward_target_without_crossing():
    for target in map(D, ("0", ".1", ".5", ".9", "1")):
        for btc, quote in ((D(".003"), D("0")), (D("0"), D("240")), (D(".001"), D("100"))):
            answer = order(btc_balance=btc, quote_balance=quote, target_btc_fraction=target, slippage_bps="3")
            if answer["status"] != "READY":
                continue
            before = btc * 80000 / (btc * 80000 + quote)
            b, q = D(answer["btc_after"]), D(answer["quote_after"])
            after = b * 80000 / (b * 80000 + q)
            assert b >= 0 and q >= 0
            assert abs(after - target) <= abs(before - target)
            if answer["side"] == "BUY":
                assert before <= after <= target + D("1e-25")
            else:
                assert target - D("1e-25") <= after <= before


def test_public_client_blocks_mutating_private_and_unknown_queries_before_network():
    client = PublicClient()
    client.opener = Mock()
    for market, path, params in [("spot", "/api/v3/order", {}), ("spot", "/api/v3/account", {}),
        ("spot", "/api/v3/time", {"signature": "no"}), ("usdm", "/fapi/v1/ticker/bookTicker", {"symbol": "ETHUSDT"}),
        ("coinm", "/dapi/v1/ticker/bookTicker", {})]:
        with pytest.raises(ValueError):
            client.get(market, path, params)
    client.opener.open.assert_not_called()


@pytest.mark.parametrize("name", ["public_snapshot.json", "market_fit_report.json",
    "exchange_info_BTCUSDT_spot.json", "exchange_info_BTCUSDC_spot.json"])
def test_scan_preserves_existing_artifact_before_any_network(tmp_path, name):
    (tmp_path / name).write_text("original")
    client = Mock()
    with pytest.raises(FileExistsError):
        scan(tmp_path, client=client)
    client.get.assert_not_called()
    assert (tmp_path / name).read_text() == "original"


def fake_public_client():
    def get(market, path, params=None):
        params = params or {}
        symbol = params.get("symbol", "BTCUSD_PERP" if market == "coinm" else "BTCUSDT")
        if path.endswith("/time"):
            return {"serverTime": 1}
        if path.endswith("/exchangeInfo"):
            symbols = [symbol] if market != "usdm" else ["BTCUSDT", "BTCUSDC"]
            return {"symbols": [{"symbol": s, "status": "TRADING", "baseAsset": "BTC",
                "contractType": "PERPETUAL", "contractSize": 100, "filters":
                filters(minimum="1", step="1", notional="0") if market == "coinm" else filters()}
                for s in symbols]}
        if path.endswith("/bookTicker"):
            return {"symbol": symbol, "bidPrice": "80000", "askPrice": "80000", "bidQty": "100", "askQty": "100"}
        if path.endswith("/referencePrice"):
            return {"referencePrice": "80000"}
        if path.endswith("/avgPrice"):
            return {"mins": 5, "price": "80000"}
        if path.endswith("/premiumIndex"):
            return {"markPrice": "80000"}
        raise AssertionError(path)
    return Mock(get=Mock(side_effect=get))


def test_scan_exports_standalone_spot_metadata_for_reproducible_research(tmp_path):
    scan(tmp_path, client=fake_public_client())
    for symbol in ("BTCUSDT", "BTCUSDC"):
        saved = json.loads((tmp_path / f"exchange_info_{symbol}_spot.json").read_text())
        assert saved["symbols"][0]["symbol"] == symbol
        assert saved["symbols"][0]["filters"][0]["stepSize"] == "0.00001"
        assert saved["retrieved_utc"]
        assert saved["public_source"].endswith("symbol=" + symbol)


def test_hypothetical_usdm_collateral_conversion_does_not_spend_btc_dust(tmp_path):
    report = scan(tmp_path, client=fake_public_client(), slippage_bps=D("0"))
    spot = next(row for row in report["markets"] if row["market"] == "spot" and row["symbol"] == "BTCUSDT")
    future = next(row for row in report["markets"] if row["market"] == "usdm" and row["symbol"] == "BTCUSDT")
    budget = spot["budgets"]["whole_wallet_comparison_only"]
    assert D(budget["all_to_quote_from_btc"]["quantity"]) == D(".00412")
    assert D(budget["btc_dust_after_conversion_not_quote_collateral"]) == D(".00000273")
    expected = D(".00412") * D("80000") * D(".999")
    assert D(budget["hypothetical_quote_after_btc_conversion"]) == expected
    other = future["budgets"]["whole_wallet_comparison_only"]
    assert D(other["quote_budget_or_conversion_value"]) == expected
    assert D(other["btc_dust_after_conversion_not_quote_collateral"]) == D(".00000273")
