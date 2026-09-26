"""Scoring and the pass rule of the Gemini historical test."""
from btc_lab import llm_backtest as lb


def items_and_rows(calls):
    """calls: (gemini 4h direction, realized 4h move, swing direction)."""
    items, rows = [], {}
    for t, (d, move, swing) in enumerate(calls):
        items.append((t, {"swing": {"direction": swing}}, None, move, move))
        rows[t] = {"decision": {"direction_1h": d, "direction_4h": d}}
    return items, rows


def test_a_model_that_matches_always_long_does_not_pass():
    items, rows = items_and_rows([("long", 0.01, 1), ("long", -0.01, 1), ("long", 0.02, 1), ("long", -0.005, 1)])
    s = lb.score(items, rows)
    assert s["4h"]["hit"] == s["4h"]["always_long_hit"] == 0.5
    assert not s["passes"]


def test_a_model_beating_both_baselines_by_three_points_passes():
    calls = [("long", 0.01, 1), ("short", -0.01, 1), ("long", 0.02, -1), ("flat", 0.03, 1), ("short", -0.02, 1)]
    items, rows = items_and_rows(calls)
    s = lb.score(items, rows)["4h"]
    assert s["taken"] == 4 and s["hit"] == 1.0
    assert s["always_long_hit"] == 0.6 and s["swing_hit"] == 0.4
    assert lb.score(items, rows)["passes"]


def test_errors_and_missing_answers_are_not_counted():
    items, rows = items_and_rows([("long", 0.01, 1), ("short", 0.01, 1)])
    rows[1] = {"error": "RuntimeError: Gemini HTTP 429"}
    s = lb.score(items, rows)["4h"]
    assert s["calls"] == 1 and s["taken"] == 1
