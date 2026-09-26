"""Rebinding the live ledger to new code without touching positions."""
from decimal import Decimal as D
import json
import sqlite3
from types import SimpleNamespace

import pytest

from btc_portfolio.code_update import BINDING_ONLY, rebind, rebind_blockers


def account(amount, algos=()):
    return {"position": SimpleNamespace(position_amt=D(amount)),
            "algos": [SimpleNamespace(client_id=a) for a in algos]}


STATE = {"binding": {"identity": "old", "mode": "live"}, "coin_qty": "3", "stop": {"id": "bsg_stop"}}


def test_only_the_binding_change_may_remain():
    ready = {"blockers": sorted(BINDING_ONLY)}
    assert rebind_blockers(STATE, account(3, ["bsg_stop"]), ready, True) == []
    other = {"blockers": ["portfolio_ledger_binding_changed", "open_orders_present"]}
    assert rebind_blockers(STATE, account(3, ["bsg_stop"]), other, True) == ["open_orders_present"]


@pytest.mark.parametrize("state,acct,stopped,reason", [
    (STATE, account(3, ["bsg_stop"]), False, "trading_services_must_be_stopped"),
    (STATE, account(2, ["bsg_stop"]), True, "ledger_coinm_quantity_differs_from_exchange"),
    (STATE, account(3, ["bsg_other"]), True, "ledger_protective_stop_not_on_exchange"),
    ({**STATE, "stop": None}, account(3), True, "ledger_protective_stop_not_on_exchange"),
    ({**STATE, "halt": "x"}, account(3, ["bsg_stop"]), True, "existing_halt_requires_resolution"),
])
def test_position_stop_and_service_are_checked(state, acct, stopped, reason):
    assert reason in rebind_blockers(state, acct, {"blockers": []}, stopped)


def test_flat_account_needs_no_stop():
    assert rebind_blockers({**STATE, "coin_qty": "0", "stop": None}, account(0), {"blockers": []}, True) == []


def ledger(path):
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                         "CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, created_ms INTEGER NOT NULL);")
        for k, v in {"binding": {"identity": "old", "mode": "live"}, "coin_qty": "3",
                     "aggressive_initial_equity": ".003", "wallet": {"BTC": ".001"}}.items():
            db.execute("INSERT INTO state VALUES(?,?)", (k, json.dumps(v)))


def test_rebind_changes_only_the_binding_and_keeps_a_backup(tmp_path):
    path = tmp_path/"ledger.sqlite3"
    ledger(path)
    rebind(path, "old", "new", tmp_path/"backup.sqlite3", {"files": {}})
    with sqlite3.connect(path) as db:
        state = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM state")}
        kinds = [r[0] for r in db.execute("SELECT kind FROM events")]
    assert state["binding"] == {"identity": "new", "mode": "live"}
    assert state["coin_qty"] == "3" and state["aggressive_initial_equity"] == ".003" and state["wallet"] == {"BTC": ".001"}
    assert kinds == ["code_rebind"]
    with sqlite3.connect(tmp_path/"backup.sqlite3") as db:
        assert json.loads(db.execute("SELECT value FROM state WHERE key='binding'").fetchone()[0])["identity"] == "old"


def test_rebind_refuses_a_stale_expected_identity_and_existing_backup(tmp_path):
    path = tmp_path/"ledger.sqlite3"
    ledger(path)
    with pytest.raises(ValueError):
        rebind(path, "not-old", "new", tmp_path/"b1.sqlite3", {})
    (tmp_path/"b2.sqlite3").write_text("x")
    with pytest.raises(ValueError):
        rebind(path, "old", "new", tmp_path/"b2.sqlite3", {})
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT value FROM state WHERE key='binding'").fetchone()[0])["identity"] == "old"
