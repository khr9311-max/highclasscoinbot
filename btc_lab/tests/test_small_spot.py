"""Accounting and causality checks for the independent public-data spot study."""
import ast
from decimal import Decimal as D
import hashlib
import inspect
import json

import pytest

from btc_lab import small_spot as spot


def filters(notional="5", step="0.00001"):
    return [{"filterType": "LOT_SIZE", "minQty": step, "maxQty": "9000", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "9000", "stepSize": "0"},
            {"filterType": "NOTIONAL", "minNotional": notional, "applyMinToMarket": True,
             "maxNotional": "9000000", "applyMaxToMarket": False, "avgPriceMins": 5}]


def bar(t, opening=80000, close=None):
    close = opening if close is None else close
    return spot.SpotBar(t, opening, max(opening, close), min(opening, close), close)


def config(**changes):
    return spot.SpotConfig(slippage_bps=0, **changes)


def test_constant_price_roundtrip_conserves_received_asset_fees_and_dust():
    result = spot.simulate_spot([bar(0), bar(3600)], {0: 0}, filters(), config())
    summary = result["summary"]
    sell, buy = result["events"]
    assert sell["side"] == "SELL" and buy["side"] == "BUY"
    assert D(sell["fee_btc"]) == 0 < D(buy["fee_btc"])
    assert D(sell["fee_quote_usdt"]) > 0 == D(buy["fee_quote_usdt"])
    loss = .003 - summary["final_net_btc"]
    assert loss == pytest.approx(summary["fees_paid_btc"] + summary["fees_paid_usdt"] / 80000)
    assert summary["ledger_btc_error"] == summary["ledger_usdt_error"] == 0
    assert summary["final_btc"] == summary["final_net_btc"]
    assert summary["final_net_btc"] == pytest.approx(summary["final_held_btc"] + summary["quote_dust_valued_btc"])
    assert summary["quote_dust_usdt"] > 0


def test_btc_hold_has_zero_btc_return_regardless_of_price_path():
    bars = [bar(0, 80000, 40000), bar(3600, 40000, 100000)]
    result = spot.simulate_spot(bars, {b.t: 1 for b in bars}, filters())
    summary = result["summary"]
    assert summary["return_pct"] == summary["max_drawdown_pct"] == summary["fees_paid_btc"] == 0
    assert summary["final_held_btc"] == .003
    assert not result["events"]


def test_cash_gains_btc_valuation_on_price_halving_only_terminal_conversion_realizes_it():
    result = spot.simulate_spot([bar(0, 80000), bar(3600, 40000)], {0: 0}, filters(), config(fee_rate=0))
    before = result["pre_terminal_conversion"]
    assert D(before["held_btc"]) == 0
    assert D(before["quote_usdt"]) == 240
    assert D(before["net_btc"]) == D(".006")
    assert result["equity_curve"][1]["held_btc"] == 0
    assert result["summary"]["terminal_realizable_btc"] == .006
    assert result["summary"]["return_pct"] == 100
    assert result["events"][-1]["reason"] == "terminal_conversion"
    # Synthetic one-hour gains can overflow float annualization; never emit NaN/Infinity.
    assert result["summary"]["cagr_pct"] is None
    json.dumps(result, allow_nan=False)


def test_cash_loses_btc_valuation_on_price_doubling_without_usdt_loss():
    result = spot.simulate_spot([bar(0, 40000), bar(3600, 80000)], {0: 0}, filters(), config(fee_rate=0))
    assert D(result["pre_terminal_conversion"]["quote_usdt"]) == 120
    assert result["summary"]["return_pct"] == -50
    assert result["summary"]["max_drawdown_pct"] == 50


def test_small_cash_residue_is_not_invented_into_a_tradable_buy():
    result = spot.simulate_spot([bar(0)], {0: .99}, filters(), config(fee_rate=0))
    # Selling the desired 2.4 USDT is below minNotional; no upsize to 5 USDT.
    assert not result["events"]
    assert result["summary"]["final_held_btc"] == .003
    assert result["summary"]["skip_reasons"]


def test_dust_below_minimum_is_value_not_terminal_held_btc():
    result = spot.simulate_spot([bar(0)], {0: 0}, filters(), config())
    summary = result["summary"]
    assert 0 < summary["quote_dust_usdt"] < 5
    assert summary["terminal_realizable_btc"] < summary["final_net_btc"]
    # A second conversion with only dust cannot buy the exchange minimum.
    from btc_lab.market_fit import size_spot_order
    remaining = size_spot_order(btc_balance=str(summary["final_held_btc"]),
                               quote_balance=str(summary["quote_dust_usdt"]), target_btc_fraction=1,
                               bid=80000, ask=80000, filters=filters(), fee_rate=.001, slippage_bps=0)
    assert remaining["status"] == "SKIP"


def test_band_is_overridden_by_forced_terminal_conversion():
    result = spot.simulate_spot([bar(0), bar(3600)], {0: .04, 3600: .01}, filters(),
                                config(fee_rate=0, no_trade_band=.05))
    assert result["summary"]["band_skips"] == 1
    assert result["summary"]["terminal_conversion_attempts"] == 1
    assert result["events"][-1]["reason"] == "terminal_conversion"
    assert result["events"][-1]["side"] == "BUY"
    assert result["summary"]["final_held_btc"] == .003


def test_terminal_conversion_splits_size_caps_until_only_dust_remains():
    limits = filters()
    limits[0]["maxQty"] = ".001"
    result = spot.simulate_spot([bar(0, 100000), bar(3600, 100000)], {0: 0, 3600: 0},
                                limits, config(fee_rate=0))
    final_buys = [event for event in result["events"] if event["reason"] == "terminal_conversion"]
    assert len(final_buys) == 2
    assert result["summary"]["final_held_btc"] == .003
    assert result["summary"]["remaining_quote_usdt"] == 0
    assert result["summary"]["terminal_conversion_status"] == "completed"


def test_terminal_conversion_cap_does_not_mislabel_tradeable_quote_as_dust(monkeypatch):
    monkeypatch.setattr(spot, "TERMINAL_MAX_ORDERS", 1)
    limits = filters()
    limits[0]["maxQty"] = ".001"
    result = spot.simulate_spot([bar(0, 100000), bar(3600, 100000)], {0: 0, 3600: 0},
                                limits, config(fee_rate=0))
    summary = result["summary"]
    assert summary["terminal_conversion_status"] == "order_cap_reached"
    assert summary["terminal_residual_tradeable"] is True
    assert summary["remaining_quote_usdt"] == summary["unconverted_tradeable_quote_usdt"] == 100
    assert summary["quote_dust_usdt"] == 0


def test_missing_hour_does_not_create_or_delay_an_order():
    result = spot.simulate_spot([bar(0), bar(7200)], {3600: 0}, filters())
    assert result["summary"]["missing_execution_hours"] == 1
    assert result["summary"]["unobserved_target_count"] == 1
    assert result["unobserved_target_times"] == [3600]
    assert not result["events"]
    assert [p["t"] for p in result["equity_curve"]] == [0, 3600, 10800]


@pytest.mark.parametrize("family", ["momentum_fraction", "ema20_100_binary", "hold_btc"])
def test_completed_day_targets_are_prefix_causal(family):
    days = [bar(i * 86400, 80000 + 100 * i) for i in range(240)]
    prefix = spot.target_schedule(days[:220], family)
    full = spot.target_schedule(days, family)
    perturbed = [*days[:220], *[bar(i * 86400, 1000 + i) for i in range(220, 240)]]
    future_changed = spot.target_schedule(perturbed, family)
    assert prefix
    assert all(full[t] == weight == future_changed[t] for t, weight in prefix.items())
    assert min(prefix) == days[200].t + 86400
    assert max(prefix) == days[219].t + 86400


def test_signal_day_close_cannot_trade_during_that_same_day():
    days = [bar(i * 86400, 80000 - 10 * i) for i in range(202)]
    targets = spot.target_schedule(days, "momentum_fraction")
    assert days[200].t not in targets
    assert targets[days[200].t + 86400] == 0
    changed = list(days)
    changed[201] = bar(days[201].t, 200000)
    assert spot.target_schedule(changed, "momentum_fraction")[days[200].t + 86400] == 0


def test_missing_daily_observations_break_warmup_guard():
    days = [bar(i * 86400, 80000 + i) for i in range(240) if i != 210]
    schedule = spot.target_schedule(days, "momentum_fraction")
    assert schedule and max(schedule) == 210 * 86400


@pytest.mark.parametrize("bars", [[bar(0), bar(0)], [bar(3600), bar(0)], [bar(1)],
                                  [bar(0, float("nan"))], [bar(0, -1)]])
def test_bad_bars_are_rejected(bars):
    with pytest.raises(ValueError):
        spot.simulate_spot(bars, {}, filters())


@pytest.mark.parametrize("updates", [{"fee_rate": float("nan")}, {"initial_btc": float("inf")},
                                     {"slippage_bps": -1}, {"no_trade_band": 1.1},
                                     {"fee_rate": True}, {"fee_asset_mode": "BNB"}])
def test_nonfinite_or_invalid_financial_inputs_rejected(updates):
    with pytest.raises(ValueError):
        spot.SpotConfig(**updates)


@pytest.mark.parametrize("targets", [{0: float("nan")}, {0: 1.1}, {1: 0}, {True: 0}])
def test_bad_targets_are_rejected(targets):
    with pytest.raises(ValueError):
        spot.simulate_spot([bar(0)], targets, filters())


def test_hourly_csv_allows_gaps_but_rejects_open_candle(tmp_path):
    path = tmp_path / "hours.csv"
    path.write_text("open_time_ms,open,high,low,close\n0,1,1,1,1\n7200000,2,2,2,2\n", encoding="utf-8")
    assert len(spot.read_spot_csv(path, now=10800)) == 2
    with pytest.raises(ValueError, match="unfinished"):
        spot.read_spot_csv(path, now=10799)


def test_actual_daily_reader_requires_contiguous_days(tmp_path):
    path = tmp_path / "days.csv"
    path.write_text("open_time_ms,open,high,low,close\n0,1,1,1,1\n172800000,2,2,2,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        spot.read_spot_csv(path, now=400000, period_sec=86400)


def test_optional_quote_fee_mode_retains_explicit_accounting():
    result = spot.simulate_spot([bar(0), bar(3600)], {0: 0}, filters(), config(fee_asset_mode="quote"))
    assert result["summary"]["fees_paid_btc"] == 0
    assert result["summary"]["fees_paid_usdt"] > 0
    assert result["summary"]["ledger_btc_error"] == result["summary"]["ledger_usdt_error"] == 0


def test_adverse_slippage_and_stress_reduce_constant_price_net_btc():
    bars = [bar(0), bar(3600), bar(7200)]
    targets = {0: 0, 3600: 1, 7200: 0}
    base = spot.simulate_spot(bars, targets, filters(), spot.SpotConfig())
    stress = spot.simulate_spot(bars, targets, filters(), spot.SpotConfig(fee_rate=.002, slippage_bps=6))
    assert stress["summary"]["final_net_btc"] < base["summary"]["final_net_btc"] < .003
    assert all(D(e["equity_change_at_reference_btc"]) < 0 for e in base["events"])
    assert all(D(e["btc_after"]) >= 0 and D(e["quote_after"]) >= 0 for e in stress["events"])


def test_public_client_is_never_constructed_and_order_modules_are_not_imported(monkeypatch):
    from btc_lab import market_fit
    def forbidden(*args, **kwargs):
        raise AssertionError("Research must not construct any exchange client")
    monkeypatch.setattr(market_fit, "PublicClient", forbidden)
    spot.simulate_spot([bar(0)], {0: 0}, filters())
    tree = ast.parse(inspect.getsource(spot))
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any("binance_coinm" in name or "forward" in name or "execution" in name for name in imports)


def test_protocol_is_written_before_computing_any_returns(tmp_path, monkeypatch):
    candles = tmp_path / "hours.csv"
    daily = tmp_path / "days.csv"
    info = tmp_path / "symbol.json"
    quality = tmp_path / "quality.json"
    output = tmp_path / "output"
    candles.write_text("test hourly bytes", encoding="utf-8")
    daily.write_text("test daily bytes", encoding="utf-8")
    info.write_text(json.dumps({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
                               "status": "TRADING", "filters": filters()}), encoding="utf-8")
    quality.write_text(json.dumps({"symbol": "BTCUSDT", "datasets": {
        key: {"data_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for key, path in (("1h", candles), ("1d", daily))}}), encoding="utf-8")
    monkeypatch.setattr(spot, "read_spot_csv", lambda *a, **kw: [bar(spot.stamp("2021-04-01")), bar(spot.stamp("2025-01-01"))])
    def stop_before_returns(*args, **kwargs):
        protocol = json.loads((output / "protocol.json").read_text(encoding="utf-8"))
        assert len(protocol["cases"]) == 4
        assert protocol["initial_btc"] == .003
        assert protocol["fee_asset_mode"] == "received"
        assert not list(output.glob("*base.json"))
        raise RuntimeError("Protocol verified before simulation")
    monkeypatch.setattr(spot, "target_schedule", stop_before_returns)
    with pytest.raises(RuntimeError, match="Protocol verified"):
        spot.run_study(candles, info, output, daily_data=daily, quality_manifest=quality)
