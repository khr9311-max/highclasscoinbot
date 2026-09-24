import json
from pathlib import Path
import shutil
from unittest.mock import Mock

import pytest

from btc_spot.registry import META_KEY, RegistryError, enforce, inspect_binding
from btc_spot.store import Store


UID = 123456789123456


def factory(path):
    return Store(path, "live", ".003", strategy_fingerprint="registry-test")


def paths(tmp_path):
    return tmp_path / "live" / "ledger.sqlite3", tmp_path / "global" / "live-registry.json"


def test_fresh_inspection_is_read_only_and_does_not_create_files(tmp_path):
    ledger, registry = paths(tmp_path)
    answer = inspect_binding(UID, ledger, registry)
    assert answer["status"] == "UNREGISTERED" and not answer["initialized"]
    assert answer["wallet"] is None and answer["pending_count"] == 0
    assert not ledger.exists() and not registry.exists() and not ledger.parent.exists()


def test_first_registration_binds_uuid_identity_and_canonical_path(tmp_path):
    ledger, registry = paths(tmp_path)
    with enforce(UID, ledger, registry, factory) as store:
        record = json.loads(registry.read_text())
        assert store._get(META_KEY) == record
        assert record["ledger_uuid"]
        assert Path(record["ledger_path"]).resolve() == ledger.resolve()
    assert str(UID) not in registry.read_text()
    answer = inspect_binding(UID, ledger, registry)
    assert answer["status"] == "REGISTERED" and not answer["initialized"]
    with enforce(str(UID), ledger, registry, factory) as store:
        assert store._get(META_KEY)["ledger_uuid"] == answer["ledger_uuid"]


def test_resumption_inspection_returns_current_wallet_without_raw_uid(tmp_path):
    ledger, registry = paths(tmp_path)
    with enforce(UID, ledger, registry, factory) as store:
        store.initialize_wallet(total_btc=".00412273", total_quote="12", account_uid=UID)
        store.create_decision("2026-09-24", "0.5", side="SELL", quantity=".0015", limit_price="80000")
        store.set_blocker("awaiting_fills")
    answer = inspect_binding(UID, ledger, registry)
    assert answer["initialized"]
    assert answer["wallet"] == {"btc": "0.003", "quote": "0", "reserve_btc": "0.00112273", "reserve_quote": "12"}
    assert answer["pending_count"] == 1 and answer["blocked"]
    assert answer["pending"][0]["decision_id"] == "2026-09-24"
    assert answer["blocker"] == {"reason": "awaiting_fills", "sticky": False}
    assert answer["initial_btc"] == "0.003" and answer["strategy_fingerprint"] == "registry-test"
    assert answer["binding"] == {"mode": "live", "symbol": "BTCUSDT", "initial_btc": "0.003", "strategy_fingerprint": "registry-test"}
    assert str(UID) not in json.dumps(answer)


def test_different_state_directory_is_rejected_before_store_factory(tmp_path):
    ledger, registry = paths(tmp_path)
    enforce(UID, ledger, registry, factory).close()
    new_ledger = tmp_path / "wrong" / "ledger.sqlite3"
    create = Mock()
    with pytest.raises(RegistryError, match="path differs"):
        enforce(UID, new_ledger, registry, create)
    create.assert_not_called()
    assert not new_ledger.exists()


def test_missing_registered_database_is_not_recreated(tmp_path):
    ledger, registry = paths(tmp_path)
    enforce(UID, ledger, registry, factory).close()
    ledger.unlink()
    create = Mock()
    with pytest.raises(RegistryError, match="missing"):
        enforce(UID, ledger, registry, create)
    create.assert_not_called()
    assert not ledger.exists()


def test_changed_uid_is_rejected_and_error_never_contains_either_uid(tmp_path):
    ledger, registry = paths(tmp_path)
    enforce(UID, ledger, registry, factory).close()
    with pytest.raises(RegistryError) as caught:
        inspect_binding(UID + 1, ledger, registry)
    assert str(UID) not in str(caught.value) and str(UID + 1) not in str(caught.value)


@pytest.mark.parametrize("uid", [None, "", True, 0, -1, "not-an-id", "1\n", 1.5])
def test_stable_uid_required_without_creating_anything(tmp_path, uid):
    ledger, registry = paths(tmp_path)
    with pytest.raises(RegistryError):
        inspect_binding(uid, ledger, registry)
    assert not registry.exists()


def test_uuid_mismatch_is_rejected(tmp_path):
    ledger, registry = paths(tmp_path)
    with enforce(UID, ledger, registry, factory) as store:
        record = store._get(META_KEY)
        record["ledger_uuid"] = "00000000-0000-4000-8000-000000000001"
        with store.transaction():
            store._set(META_KEY, record)
    with pytest.raises(RegistryError, match="UUID"):
        inspect_binding(UID, ledger, registry)


def test_existing_virgin_store_can_be_registered(tmp_path):
    ledger, registry = paths(tmp_path)
    factory(ledger).close()
    enforce(UID, ledger, registry, factory).close()
    assert inspect_binding(UID, ledger, registry)["status"] == "REGISTERED"


@pytest.mark.parametrize("state", ["wallet", "decision", "blocker"])
def test_existing_unregistered_nonvirgin_store_cannot_be_adopted(tmp_path, state):
    ledger, registry = paths(tmp_path)
    with factory(ledger) as store:
        if state == "wallet":
            store.initialize_wallet(total_btc=".004", total_quote="0", account_uid=UID)
        elif state == "decision":
            store.create_decision("2026-09-24", "1", phase="NOOP")
        else:
            store.set_blocker("old_failure")
    create = Mock()
    with pytest.raises(RegistryError, match="cannot be adopted"):
        enforce(UID, ledger, registry, create)
    create.assert_not_called()
    assert not registry.exists()


def test_missing_registry_after_real_initialization_is_not_silently_rebuilt(tmp_path):
    ledger, registry = paths(tmp_path)
    with enforce(UID, ledger, registry, factory) as store:
        store.initialize_wallet(total_btc=".00412273", total_quote="0", account_uid=UID)
    registry.unlink()
    with pytest.raises(RegistryError, match="cannot be adopted"):
        enforce(UID, ledger, registry, factory)
    assert not registry.exists()


def test_interrupted_first_registration_recovers_only_same_virgin_uuid(tmp_path, monkeypatch):
    import btc_spot.registry as module
    ledger, registry = paths(tmp_path)
    original = module._write_registry
    monkeypatch.setattr(module, "_write_registry", Mock(side_effect=OSError("simulated crash before publication")))
    with pytest.raises(OSError):
        enforce(UID, ledger, registry, factory)
    before = inspect_binding(UID, ledger, registry)
    assert before["status"] == "UNREGISTERED" and before["ledger_uuid"]
    with pytest.raises(RegistryError, match="another account"):
        inspect_binding(UID + 1, ledger, registry)
    monkeypatch.setattr(module, "_write_registry", original)
    enforce(UID, ledger, registry, factory).close()
    assert inspect_binding(UID, ledger, registry)["ledger_uuid"] == before["ledger_uuid"]


def test_registry_publication_never_overwrites_an_existing_record(tmp_path):
    from btc_spot.registry import _write_registry
    ledger, registry = paths(tmp_path)
    enforce(UID, ledger, registry, factory).close()
    before = registry.read_bytes()
    record = json.loads(before)
    record["ledger_uuid"] = "00000000-0000-4000-8000-000000000002"
    with pytest.raises(RegistryError, match="publication"):
        _write_registry(registry, record)
    assert registry.read_bytes() == before
    assert list(registry.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("content", ["", "{broken", "{}", "[]"])
def test_corrupt_registry_is_not_replaced(tmp_path, content):
    ledger, registry = paths(tmp_path)
    registry.parent.mkdir(parents=True)
    registry.write_text(content)
    with pytest.raises(RegistryError):
        enforce(UID, ledger, registry, factory)
    assert registry.read_text() == content and not ledger.exists()


def test_paper_ledger_is_never_adopted_as_live(tmp_path):
    ledger, registry = paths(tmp_path)
    Store(ledger, "paper", ".003", strategy_fingerprint="registry-test").close()
    with pytest.raises(RegistryError, match="live ledger"):
        enforce(UID, ledger, registry, factory)
    assert not registry.exists()


def test_factory_opening_different_path_is_closed_and_refused(tmp_path):
    ledger, registry = paths(tmp_path)
    wrong = tmp_path / "wrong.sqlite3"
    with pytest.raises(RegistryError, match="different ledger"):
        enforce(UID, ledger, registry, lambda _: factory(wrong))
    assert not registry.exists()
    # The failed factory Store was closed and released its lock.
    factory(wrong).close()


def test_registry_blocks_copying_matching_uuid_to_new_path(tmp_path):
    ledger, registry = paths(tmp_path)
    enforce(UID, ledger, registry, factory).close()
    copied = tmp_path / "copied.sqlite3"
    shutil.copyfile(ledger, copied)
    with pytest.raises(RegistryError, match="path differs"):
        inspect_binding(UID, copied, registry)


def test_malformed_wallet_metadata_is_rejected_without_disclosing_its_content(tmp_path):
    ledger, registry = paths(tmp_path)
    with enforce(UID, ledger, registry, factory) as store:
        with store.transaction():
            store._set("wallet", {"btc": "unsafe-untrusted-content", "quote": "0"})
    with pytest.raises(RegistryError) as caught:
        inspect_binding(UID, ledger, registry)
    assert "unsafe-untrusted-content" not in str(caught.value)
