"""Gemini shadow judge: parsing, prompt, scoring against baselines, transport."""
import asyncio
import json

import numpy as np
import pytest

from btc_lab import ledger as lg
from btc_lab import llm_judge as lj


def test_parse_accepts_only_the_schema():
    ok = lj.parse('{"direction_1h": "long", "direction_4h": "flat", "confidence": 0.6, "reason": "매수 흐름"}')
    assert ok == {"direction_1h": "long", "direction_4h": "flat", "confidence": 0.6, "reason": "매수 흐름"}
    for bad in ('{"direction_1h": "buy", "direction_4h": "flat", "confidence": 0.5}',
                '{"direction_1h": "long", "direction_4h": "flat", "confidence": 1.5}',
                '["long"]', 'not json'):
        with pytest.raises(ValueError):
            lj.parse(bad)


def test_recent_returns_step_back_from_the_last_close():
    close = np.arange(100.0, 125.0)                  # 25 bars
    r = lj.recent_returns(close, 12)                 # closes 100, 112, 124
    assert r == [pytest.approx(12.0), pytest.approx(round(12 / 112 * 100, 3))]


def test_prompt_carries_snapshot_numbers_only():
    snap = {"bar_close_ms": 1_790_406_000_000, "close": 84000.0, "mark": 84001.0, "pred": {"24": 0.001},
            "model_inputs": {"x": 1}, "swing": {"direction": 1, "gap": 0.02}}
    prompt = json.loads(lj.build_prompt(snap, np.linspace(80000, 84000, 1000)))
    assert prompt["close"] == 84000.0 and prompt["swing"]["direction"] == 1
    assert "pred" not in prompt and "model_inputs" not in prompt
    assert len(prompt["hourly_returns_pct_last_48h"]) == 48 and len(prompt["four_hour_returns_pct_last_3d"]) == 18


def test_scores_compare_with_always_long_and_swing(tmp_path):
    db = lg.open_db(tmp_path)
    rows = [(1, "long", 0.01, 1), (2, "short", -0.02, 1), (3, "long", -0.01, -1), (4, "flat", 0.03, 1)]
    for t, d, fwd, swing in rows:
        db.execute("INSERT INTO snapshots(bar_close_ms, created_ms, close, fwd_12, fwd_48, payload) VALUES(?,?,?,?,?,?)",
                   (t, t, 1.0, fwd, fwd, json.dumps({"swing": {"direction": swing}})))
        lj.store(db, t, "m", {"direction_1h": d, "direction_4h": d, "confidence": 0.5, "reason": ""})
    s = lj.scores(db, 0)["1h"]
    assert s["n"] == 4 and s["taken"] == 3
    assert s["hit"] == pytest.approx(2 / 3)                  # long up, short down right; long down wrong
    assert s["always_long_hit"] == pytest.approx(2 / 4)
    assert s["swing_hit"] == pytest.approx(3 / 4)            # swing long/long/short/long vs up/down/down/up
    db.close()


class FakeResponse:
    def __init__(self, status, data):
        self.status, self._data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._data


class FakeSession:
    def __init__(self, status, data):
        self.status, self.data, self.calls = status, data, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.status, self.data)


def test_ask_skips_thought_parts_and_hides_error_bodies():
    answer = {"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True},
        {"text": '{"direction_1h":"short","direction_4h":"short","confidence":0.4,"reason":"약세"}'}]}}]}
    session = FakeSession(200, answer)
    cfg = {"key": "secret-key", "model": "gemini-test"}
    out = asyncio.run(lj.ask(session, cfg, "{}"))
    assert out["direction_1h"] == "short"
    url, kwargs = session.calls[0]
    assert "gemini-test" in url and "secret-key" not in url and kwargs["headers"]["x-goog-api-key"] == "secret-key"
    with pytest.raises(RuntimeError) as err:
        asyncio.run(lj.ask(FakeSession(400, {"error": {"message": "echo of the request"}}), cfg, "{}"))
    assert "echo" not in str(err.value)


def test_judge_due_every_fifteen_minutes():
    assert lj.due(1_790_406_000_000) and lj.due(1_790_406_900_000) and not lj.due(1_790_406_300_000)


def test_ask_rejects_truncated_answers_and_strips_code_fences():
    cfg = {"key": "k", "model": "m"}
    cut = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"direction_1h": "lo'}]}}]}
    with pytest.raises(RuntimeError, match="MAX_TOKENS"):
        asyncio.run(lj.ask(FakeSession(200, cut), cfg, "{}"))
    fenced = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": '```json\n{"direction_1h":"flat","direction_4h":"long","confidence":0.3,"reason":"x"}\n```'}]}}]}
    assert asyncio.run(lj.ask(FakeSession(200, fenced), cfg, "{}"))["direction_4h"] == "long"
