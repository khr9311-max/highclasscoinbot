from datetime import datetime, timezone
import json

import pytest

from btc_lab.engine import Bar, Config, Spec, run
from btc_lab.forward import (DAY, HOUR, ForwardRunner, PublicClient,
                             _atomic_json, _observation)


INITIAL = int(datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp())
ACTIVATION = (INITIAL // DAY + 1) * DAY


class FakePublic:
    def __init__(self, now=INITIAL):
        self.now = now
        self.calls = []
        self.bad_marks = False
        self.fail_funding = False
        self.late_funding = False
        self.change_history = False

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        if path.endswith("/time"):
            return {"serverTime": self.now * 1000}
        if path.endswith("/exchangeInfo"):
            return {"symbols": [{"symbol": "BTCUSD_PERP", "contractStatus": "TRADING",
                "contractType": "PERPETUAL", "marginAsset": "BTC", "quoteAsset": "USD",
                "contractSize": 100, "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
                    {"filterType": "PRICE_FILTER", "tickSize": ".1"}]}]}
        if path.endswith("/fundingRate"):
            if self.fail_funding:
                raise RuntimeError("network failure")
            return ([{"symbol": "BTCUSD_PERP", "fundingTime": ACTIVATION * 1000,
                      "fundingRate": ".001", "markPrice": "70000"}] if self.late_funding else [])
        period = DAY if params["interval"] == "1d" else HOUR
        start, end = params["startTime"] // 1000, (params["endTime"] + 1) // 1000
        rows = []
        for t in range(start, end, period):
            day = (t - (INITIAL // DAY * DAY - 350 * DAY)) / DAY
            opening = 50_000 * 1.001 ** day
            closing = 50_000 * 1.001 ** (day + period / DAY)
            if self.change_history and period == HOUR and t == ACTIVATION:
                opening *= 1.001
            prices = (opening, max(opening, closing) * 1.001,
                      min(opening, closing) * .999, closing)
            rows.append([t * 1000, *(str(p) for p in prices), "1", (t + period) * 1000 - 1])
        if self.bad_marks and path.endswith("markPriceKlines"):
            rows = rows[:-1]
        return rows


def runner(path, **kw):
    return ForwardRunner(path, .007, .0005, warmup_days=201, **kw)


def test_initialization_warms_closed_days_but_never_trades_history(tmp_path):
    public = FakePublic()
    with runner(tmp_path) as instance:
        state = instance.refresh(public)
    assert state["activation_time"] == ACTIVATION
    assert state["bars"] == []
    assert state["observation"] is None
    assert len(state["days"]) == 201
    assert state["days"][-1]["t"] + DAY <= INITIAL
    assert not any(params.get("interval") == "1h" for _, params in public.calls)
    assert not state["manifest"]["orders_enabled"]


def test_repeat_poll_only_checks_server_clock_and_does_not_duplicate(tmp_path):
    public = FakePublic()
    with runner(tmp_path) as instance:
        first = instance.refresh(public)
        public.calls.clear()
        second = instance.refresh(public)
    assert [path for path, _ in public.calls] == ["/dapi/v1/time"]
    assert first == second


def test_restart_replays_identical_ledger_and_keeps_open_position(tmp_path):
    public = FakePublic()
    with runner(tmp_path) as instance:
        instance.refresh(public)
        public.now = ACTIVATION + HOUR + 31
        first = instance.refresh(public)
    assert first["observation"]["position_contracts"] > 0
    assert all(row.get("reason") != "end_of_data" for row in first["observation"]["ledger"])
    with runner(tmp_path) as restarted:
        same = restarted.refresh(public)
        assert same == first
        public.now += HOUR
        later = restarted.refresh(public)
    assert later["observation"]["ledger"] == first["observation"]["ledger"]
    assert later["observation"]["fee_btc"] == first["observation"]["fee_btc"]
    assert later["observation"]["equity_btc"] > first["observation"]["equity_btc"]
    assert len(later["bars"]) == 2


def test_unfinished_hour_and_settlement_delay_are_excluded(tmp_path):
    public = FakePublic()
    with runner(tmp_path) as instance:
        instance.refresh(public)
        public.now = ACTIVATION + HOUR + 20
        early = instance.refresh(public)
        assert early["bars"] == []
        public.now = ACTIVATION + HOUR + 30
        closed = instance.refresh(public)
        assert len(closed["bars"]) == 1
        assert closed["bars"][0]["t"] == ACTIVATION


@pytest.mark.parametrize("failure", ["bad_marks", "fail_funding", "change_history", "late_funding"])
def test_bad_or_revised_public_input_preserves_committed_state(tmp_path, failure):
    public = FakePublic()
    with runner(tmp_path) as instance:
        instance.refresh(public)
        public.now = ACTIVATION + HOUR + 31
        instance.refresh(public)
        before = instance.path.read_bytes()
        public.now += HOUR
        setattr(public, failure, True)
        with pytest.raises((ValueError, RuntimeError)):
            instance.refresh(public)
        assert instance.path.read_bytes() == before


def test_parameter_changes_and_corruption_refuse_existing_ledger(tmp_path):
    with runner(tmp_path) as instance:
        instance.refresh(FakePublic())
    with ForwardRunner(tmp_path, .008, .0005, warmup_days=201) as changed:
        with pytest.raises(ValueError, match="parameters or source"):
            changed.refresh(FakePublic())
    path = tmp_path / "state.json"
    state = json.loads(path.read_text())
    state["created_server_ms"] += 1
    path.write_text(json.dumps(state), encoding="utf-8")
    with runner(tmp_path) as corrupt:
        with pytest.raises(ValueError, match="integrity"):
            corrupt.refresh(FakePublic())


def test_instance_lock_blocks_duplicate_runner_and_releases_on_exit(tmp_path):
    with runner(tmp_path):
        with pytest.raises(RuntimeError):
            with runner(tmp_path):
                pass
    with runner(tmp_path) as restarted:
        assert restarted.refresh(FakePublic())["bars"] == []


def test_atomic_write_failure_leaves_previous_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    _atomic_json(path, {"committed": 1})
    before = path.read_bytes()
    def reject(source, destination):
        raise OSError("simulated replacement failure")
    monkeypatch.setattr("btc_lab.forward.os.replace", reject)
    with pytest.raises(OSError):
        _atomic_json(path, {"committed": 2})
    assert path.read_bytes() == before


def test_public_client_refuses_order_endpoint_and_auth_parameters():
    client = PublicClient()
    for path, params in [("/dapi/v1/order", {}), ("/dapi/v1/time", {"signature": "forbidden"}),
                         ("https://other.example/path", {}),
                         ("/dapi/v1/klines", {"symbol": "ETHUSD_PERP"})]:
        with pytest.raises(ValueError):
            client.get(path, params)


@pytest.mark.parametrize("direction", [1, -1])
def test_removing_terminal_close_reconstructs_marked_wallet_exactly(direction):
    bars = [Bar(0, 50_000, 50_000, 50_000, 50_000, 50_000, 50_000, 50_000, 50_000),
            Bar(HOUR, 55_000, 55_000, 55_000, 55_000, 55_000, 55_000, 55_000, 55_000)]
    cfg = Config(initial_btc=1, fee=.001, slip_bps=0, stop_slip_bps=0)
    raw = run(bars, [], {0: direction}, Spec(), cfg)
    observation = _observation(raw, Spec())
    opening = observation["ledger"][0]
    cash = 1 - opening["fee_btc"]
    expected_equity = cash + opening["qty"] * 100 * (1 / opening["price"] - 1 / 55_000)
    assert observation["cash_btc"] == pytest.approx(cash)
    assert observation["equity_btc"] == pytest.approx(expected_equity)
    assert observation["closed_trades"] == []
    assert observation["growth"]["terminal_equity_btc"] == observation["equity_btc"]
