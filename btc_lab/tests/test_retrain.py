"""Monthly retrain: windows, gate, promotion and the data window defaults."""
from datetime import datetime, timezone
import json

import numpy as np

from btc_lab import flow_data
from btc_lab import intraday_data as idata
from btc_lab import ledger as lg
from btc_lab import regime_switch as rs
from btc_lab import retrain as rt


def test_window_trains_through_the_last_complete_month():
    w = rt.window_for(datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc))
    assert w["end_ms"] == rt.ms(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert w["last_month"] == (2026, 9) and w["probe_month"] == "2026-09"
    assert w["probe_start_ms"] == rt.ms(datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert rt.window_for(datetime(2027, 1, 3, tzinfo=timezone.utc))["last_month"] == (2026, 12)


def probe(ic=0.03, corr=0.8):
    return {"24": {"rank_ic": ic, "pred_corr_with_live": corr}, "12": {"rank_ic": ic, "pred_corr_with_live": corr}}


CURRENT = {"features": list(rs.LGBM_FEATURES), "train_end_ms": 1_790_294_400_000}


def test_gate_passes_only_when_every_check_holds():
    newer = 1_790_294_400_000 + 1
    assert rt.gate(CURRENT, newer, probe(), [])[1]
    for args in ((CURRENT, 1_790_294_400_000, probe(), []),          # not newer than live
                 (CURRENT, newer, probe(ic=-0.06), []),               # broken predictive power
                 (CURRENT, newer, probe(corr=0.2), []),               # gross drift from the live model
                 (CURRENT, newer, probe(), ["funding_incomplete"]),   # incomplete archives
                 ({**CURRENT, "features": ["x"]}, newer, probe(), [])):
        assert not rt.gate(*args)[1]
    assert rt.gate(CURRENT, newer, probe(ic=-0.05), [])[1]           # the boundary itself passes


def test_promote_keeps_history_and_replaces_atomically(tmp_path):
    path = tmp_path / "model" / "ledger_model.json"
    lg.write_model(path, {"train_end_ms": 1, "v": "old"})
    rt.promote(path, {"train_end_ms": 2, "v": "new"})
    assert json.loads(path.read_text())["v"] == "new"
    assert json.loads((tmp_path / "model/history/ledger_model-1.json").read_text())["v"] == "old"
    assert not list(path.parent.glob("*.tmp"))


def test_message_names_failed_checks_and_dry_runs():
    report = {"probe": {"24": {"rank_ic": 0.031, "n": 360, "pred_corr_with_live": 0.8},
                        "12": {"rank_ic": 0.02, "n": 720, "pred_corr_with_live": 0.8}},
              "promoted": False, "dry_run": False, "passed": False, "window": {"probe_month": "2026-09"},
              "checks": {"newer_than_live": False, "archives_complete": True},
              "run_utc": "2026-10-03T03:30:00+00:00", "candidate_train_end_ms": 1_790_812_800_000,
              "current_train_end_ms": 1_790_294_400_000}
    text = rt.message(report)
    assert "newer_than_live" in text and "~2026-09-30" in text and "매매 규칙은 바꾸지 않는다" in text
    assert "시험 실행" in rt.message({**report, "dry_run": True, "passed": True})


def test_data_window_defaults_are_the_published_study_window():
    assert flow_data.DEFAULT.start_ms == flow_data.START_MS and flow_data.DEFAULT.end_ms == idata.REST_END
    assert len(flow_data.grid()) == 550_656
    months = list(flow_data.months())
    assert months[0] == "2021-07" and months[-1] == "2026-08"
    assert list(idata.months())[0] == "2021-01" and list(idata.months())[-1] == "2026-08"


def test_archive_only_window_never_calls_rest(monkeypatch):
    calls = []
    monkeypatch.setattr(idata, "fetch", lambda url, attempts=6: calls.append(url) or None)
    w = flow_data.Window(start_ms=flow_data.START_MS, end_ms=flow_data.START_MS + 86_400_000,
                         first_month=(2021, 7), last_month=(2021, 7), rest_start=None)
    part, missing = flow_data.build_klines("cm", flow_data.grid(w), w)
    assert missing == ["2021-07"] and all("data.binance.vision" in u for u in calls)
    assert np.isnan(part["cm_volume"]).all()
