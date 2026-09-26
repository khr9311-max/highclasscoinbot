"""Execution timing of aggressive COIN-M flips and entries from the ledger prediction."""
import asyncio
from decimal import Decimal as D
import json

import pytest

from btc_portfolio import timing
from btc_portfolio.aggressive import execute
from btc_portfolio.config import Config
from btc_portfolio.engine import Engine
from btc_portfolio.store import Store
from btc_portfolio.tests.test_aggressive import _safe, coin_market
from btc_portfolio.tests.test_portfolio import FakeVenues, spot_market


class State(dict):
    def put(self, key, value):
        self[key] = value


def write(path, bar_close_ms, pred):
    path.write_text(json.dumps({"bar_close_ms": bar_close_ms, "pred_24": pred}))


def cfg(path, wait=14400):
    return Config(strategy_mode="aggressive", timing_prediction_path=str(path), timing_max_wait_seconds=wait)


COIN = {"bar": str(1_790_380_800_000 - 14_400_000), "direction": 1}   # 4h bar closing at 00:00 UTC
CLOSE = 1_790_380_800_000


def test_waits_for_a_favourable_prediction(tmp_path):
    path, state = tmp_path/"prediction.json", State()
    write(path, CLOSE + 600_000, -0.0004)
    go, info = timing.decide(state, cfg(path), COIN, 1, CLOSE + 600_000)
    assert not go and info["timing"] == "waiting" and state["timing_wait"]["since_ms"] == CLOSE
    go, info = timing.decide(state, cfg(path), COIN, -1, CLOSE + 600_000)
    assert go and info["timing"] == "favourable"          # the same forecast favours selling


def test_deadline_is_kept_across_new_bars_and_then_forces_the_trade(tmp_path):
    path, state = tmp_path/"prediction.json", State()
    write(path, CLOSE + 60_000, -0.001)
    assert not timing.decide(state, cfg(path), COIN, 1, CLOSE + 60_000)[0]
    later = {"bar": str(CLOSE), "direction": 1}                    # next 4h bar, same direction
    now = CLOSE + 14_400_000
    write(path, now, -0.001)
    go, info = timing.decide(state, cfg(path), later, 1, now)
    assert go and info["timing"] == "deadline" and info["since_ms"] == CLOSE


@pytest.mark.parametrize("payload,reason", [(None, "prediction_unreadable"), ({"bar_close_ms": CLOSE}, "prediction_unreadable"),
                                            ({"bar_close_ms": CLOSE - 700_000, "pred_24": -1}, "prediction_stale"),
                                            ({"bar_close_ms": CLOSE, "pred_24": "nan"}, "prediction_invalid")])
def test_unusable_predictions_never_delay(tmp_path, payload, reason):
    path, state = tmp_path/"prediction.json", State()
    if payload is not None:
        path.write_text(json.dumps(payload))
    go, info = timing.decide(state, cfg(path), COIN, 1, CLOSE + 1000)
    assert go and info["timing"] == reason


def test_disabled_by_default():
    assert timing.decide(State(), Config(strategy_mode="aggressive"), COIN, 1, CLOSE) == (True, {"timing": "disabled"})


def test_config_requires_aggressive_mode_and_absolute_path(tmp_path):
    with pytest.raises(ValueError):
        Config(timing_prediction_path=str(tmp_path/"p.json"))
    with pytest.raises(ValueError):
        Config(strategy_mode="aggressive", timing_prediction_path="relative/p.json")
    with pytest.raises(ValueError):
        cfg(tmp_path/"p.json", wait=20000)
    assert cfg(tmp_path/"p.json").identity() != Config(strategy_mode="aggressive").identity()


def run_execute(tmp_path, monkeypatch, position, pred, preset=None):
    path = tmp_path/"prediction.json"
    c = cfg(path)
    v = FakeVenues()
    s = Store(tmp_path/"ledger.sqlite3", "unit", "test")
    e = Engine(v, s, c)
    account = asyncio.run(v.account())
    account["spot_bnb_burn"] = False
    asyncio.run(e.initialize(account))
    if preset is not None:
        s.put("timing_wait", preset)
    v.position = D(position)
    account = asyncio.run(v.account())
    market = coin_market()                                   # rising closes: EMA direction long
    write(path, market["server_time_ms"] - 1000, pred)
    monkeypatch.setattr("btc_portfolio.aggressive.validate_actual_position", _safe)
    result = asyncio.run(execute(e, account, {"spot": {"ETHBTC": spot_market()}, "coinm": market},
                                 {"spot": {"ETHBTC": D(".001")}, "coinm": D(".0005")}, D(".003")))
    return result, [p for p in v.posts if p[1] == "coinm"], s


def test_flip_close_waits_while_the_model_expects_a_fall(tmp_path, monkeypatch):
    result, coin_posts, s = run_execute(tmp_path, monkeypatch, -1, -0.0005)
    try:
        assert coin_posts == [] and result["coinm_timing_wait"]["timing"] == "waiting"
        assert s.get("timing_wait")["direction"] == 1
    finally:
        s.close()


def test_flip_close_goes_when_the_model_favours_buying(tmp_path, monkeypatch):
    result, coin_posts, s = run_execute(tmp_path, monkeypatch, -1, 0.0005)
    try:
        assert result["action"] == "coinm_reduce_only_flip"
        assert coin_posts[0][2]["reduce_only"] and coin_posts[0][2]["side"] == "BUY"
    finally:
        s.close()


def test_entry_waits_and_does_not_block_the_rest_of_the_step(tmp_path, monkeypatch):
    result, coin_posts, s = run_execute(tmp_path, monkeypatch, 0, -0.0005)
    try:
        assert coin_posts == [] and result["coinm_timing_wait"]["side"] == 1
        assert result["status"] == "READY"
    finally:
        s.close()


def test_entry_goes_when_favoured(tmp_path, monkeypatch):
    result, coin_posts, s = run_execute(tmp_path, monkeypatch, 0, 0.0005)
    try:
        assert result.get("action") == "coinm_entry", result.get("coinm_skip")
        assert coin_posts and coin_posts[0][2]["side"] == "BUY" and not coin_posts[0][2]["reduce_only"]
        assert "coinm_timing_wait" not in result
    finally:
        s.close()


def test_aligned_position_clears_the_wait(tmp_path, monkeypatch):
    result, coin_posts, s = run_execute(tmp_path, monkeypatch, 1, -0.0005, preset={"direction": 1, "since_ms": 1})
    try:
        assert s.get("timing_wait") is None and "coinm_timing_wait" not in result
    finally:
        s.close()
