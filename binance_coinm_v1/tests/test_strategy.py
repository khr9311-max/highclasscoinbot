"""7단계: Trendy Kangaroo 롱/숏 대칭, 미래 봉 미사용, 래더 규칙."""

from decimal import Decimal

import numpy as np
import pytest

from binance_coinm_v1.strategy.ladder import (PositionLogic, better_stop, tp_quantities,
                                              tp_schedule)
from binance_coinm_v1.strategy.price_action import (Bars, Zone, atr, mirror_bars,
                                                    trendy_kangaroo, trendy_kangaroo_short)
from binance_coinm_v1.strategy.signals import SignalEngine, zones_at

from .synth import aggregate, bars_from, bullish_tk_rows, mirror_rows, random_walk

K = 400.0


def test_bullish_trendy_kangaroo_long_signal():
    b = bars_from(bullish_tk_rows())
    i = len(b) - 1
    assert trendy_kangaroo(b, i) is not None
    zones = [Zone(170.0, 169.0, 171.0, 3), Zone(180.0, 179.0, 181.0, 3)]
    ev = SignalEngine().evaluate(b, i, zones)
    s = ev.entry_for(1)
    a = atr(b, i)
    assert s is not None and s.pattern == "trendy_kangaroo"
    assert s.entry == pytest.approx(b.h[i] + 0.05 * a)       # 신호봉 고가 + 0.05 ATR
    assert s.stop == pytest.approx(b.l[i] - 0.05 * a)
    assert s.targets == [169.0, 179.0]                         # 존 하단, 가까운 순
    assert ev.entry_for(-1) is None


def test_short_is_exact_mirror_of_long():
    rows = bullish_tk_rows()
    b = bars_from(rows)
    m = bars_from(mirror_rows(rows, K))
    i = len(b) - 1
    zones = [Zone(170.0, 169.0, 171.0, 3), Zone(180.0, 179.0, 181.0, 3)]
    mzones = [Zone(K - z.price, K - z.hi, K - z.lo, z.touches) for z in zones]
    eng = SignalEngine()
    lg = eng.evaluate(b, i, zones).entry_for(1)
    sh = eng.evaluate(m, i, mzones).entry_for(-1)
    assert sh is not None and eng.evaluate(m, i, mzones).entry_for(1) is None
    assert sh.entry == pytest.approx(K - lg.entry)            # 신호봉 저가 - 0.05 ATR
    assert sh.stop == pytest.approx(K - lg.stop)              # 신호봉 고가 + 0.05 ATR
    assert sh.targets == pytest.approx([K - t for t in lg.targets])
    assert sh.atr == pytest.approx(lg.atr)
    # 반전 봉에 원본 함수를 쓰면 같은 쉬어가기 길이
    tk = trendy_kangaroo_short(mirror_bars(m), i)
    assert tk is not None and tk["pause"] == trendy_kangaroo(b, i)["pause"]


def test_mirror_invariants():
    b = random_walk(300, seed=5)
    m = mirror_bars(b)
    for i in (20, 150, 299):
        assert atr(m, i) == pytest.approx(atr(b, i))
    assert np.all(m.h == -b.l) and np.all(m.l == -b.h)


def test_directions_setting_filters():
    b = bars_from(bullish_tk_rows())
    i = len(b) - 1
    assert SignalEngine(directions=(-1,)).evaluate(b, i, []).entries == []
    assert len(SignalEngine(directions=(1,)).evaluate(b, i, []).entries) == 1


def test_risk_filter_rejects_too_wide_or_narrow():
    b = bars_from(bullish_tk_rows())
    i = len(b) - 1
    assert SignalEngine(max_risk_pct=0.001).evaluate(b, i, []).entries == []
    assert SignalEngine(min_risk_pct=0.5).evaluate(b, i, []).entries == []


def test_no_lookahead_signals_and_zones():
    ltf = random_walk(900, seed=11)
    htf = aggregate(ltf, 4)
    eng = SignalEngine()
    rng = np.random.default_rng(0)
    for i in range(100, 880, 7):
        ts = ltf.close_time(i)
        z_full = zones_at(htf, ts, 200)
        full = eng.evaluate(ltf, i, z_full)
        # 미래를 잘라내도, 미래를 엉망으로 바꿔도 같은 판정이어야 한다
        cut = ltf.upto(i + 1)
        z_cut = zones_at(htf.upto(len(htf.closed_by(ts))), ts, 200)
        garbage = Bars(ltf.t.copy(), ltf.o.copy(), ltf.h.copy(), ltf.l.copy(), ltf.c.copy(),
                       ltf.v.copy(), ltf.period)             # upto() 는 뷰라 원본이 오염된다
        garbage.h[i + 1:] = garbage.h[i + 1:] * (1 + rng.random(len(ltf) - i - 1))
        garbage.l[i + 1:] = garbage.l[i + 1:] * (1 - 0.5 * rng.random(len(ltf) - i - 1))
        a = [s.to_dict() for s in full.entries]
        assert a == [s.to_dict() for s in eng.evaluate(cut, i, z_cut).entries]
        assert [s.to_dict() for s in eng.evaluate(garbage, i, z_full).entries] == a


def test_zones_use_only_closed_higher_timeframe_bars():
    ltf = random_walk(400, seed=3)
    htf = aggregate(ltf, 4)
    ts = ltf.close_time(201)                 # 4시간봉 하나가 형성 중인 시점
    closed = htf.closed_by(ts)
    assert all(closed.t + closed.period <= ts)
    assert len(closed) == len([t for t in htf.t if t + htf.period <= ts])


# ---------------------------------------------------------------- 래더
def test_tp_quantities_cumulative_floor_never_exceeds():
    sch = tp_schedule("ladder", 3, (0.25, 0.25, 0.25))
    assert tp_quantities(3, sch) == [(1, Decimal(1)), (2, Decimal(1))]     # TP1 은 0 -> 건너뜀
    assert tp_quantities(4, sch) == [(0, Decimal(1)), (1, Decimal(1)), (2, Decimal(1))]
    assert tp_quantities(1, sch) == []
    q10 = tp_quantities(10, sch)
    assert q10 == [(0, Decimal(2)), (1, Decimal(3)), (2, Decimal(2))]
    assert sum(q for _, q in q10) == 7                                     # 3계약은 추적
    for n in range(1, 60):
        assert sum(q for _, q in tp_quantities(n, sch)) <= n
    assert tp_schedule("ladder", 1, (0.25, 0.25, 0.25)) == [(0, 0.25)]    # 목표가 하나뿐
    assert tp_schedule("ladder_ratchet", 3) == [] and tp_schedule("zone", 2) == [(0, 1.0)]


def _bar_rows(highs_lows, base=100.0):
    return [(base, h, l, (h + l) / 2) for h, l in highs_lows]


def test_ladder_ratchet_long_and_trailing():
    rows = [(100, 101, 99, 100)] * 20 + [(100, 111, 100, 110), (110, 125, 109, 124),
                                        (124, 131, 123, 130), (130, 133, 128, 132)]
    b = bars_from(rows)
    p = PositionLogic(1, 100.0, 95.0, [110.0, 120.0, 130.0], "ladder", 72)
    d1 = p.on_bar_close(b, 20)
    assert p.stop == 100.0 and d1.ladder_step == 1 and d1.new_stop == 100.0   # TP1 -> 본전
    p.on_bar_close(b, 21)
    assert p.stop == 110.0 and p.ladder_step == 2                              # TP2 -> TP1
    d3 = p.on_bar_close(b, 22)
    assert p.ladder_step == 3 and p.trailing_active                           # 3봉째 + 목표 소진
    buf = 0.05 * atr(b, 22)
    assert p.stop == pytest.approx(max(120.0, min(b.l[20:23]) - buf))
    assert d3.exit_reason is None


def test_ladder_ratchet_short_is_symmetric():
    rows = [(100, 101, 99, 100)] * 20 + [(100, 100, 89, 90), (90, 91, 75, 76)]
    b = bars_from(rows)
    p = PositionLogic(-1, 100.0, 105.0, [90.0, 80.0, 70.0], "ladder", 72)
    p.on_bar_close(b, 20)
    assert p.stop == 100.0 and p.ladder_step == 1
    p.on_bar_close(b, 21)
    assert p.stop == 90.0 and p.ladder_step == 2
    assert better_stop(-1, 90.0, 95.0) == 90.0        # 숏 손절은 내려가기만 한다


def test_exit_priority_and_time_and_stop_close():
    b = bars_from([(100, 101, 99, 100)] * 30 + [(100, 101, 90, 94)])
    p = PositionLogic(1, 100.0, 95.0, [120.0], "ladder", 72)
    assert p.on_bar_close(b, 30, reversal=True, opposite=True).exit_reason == "reversal_signal"
    p2 = PositionLogic(1, 100.0, 95.0, [120.0], "ladder", 72)
    assert p2.on_bar_close(b, 30, opposite=True).exit_reason == "opposite_signal"
    p3 = PositionLogic(1, 100.0, 95.0, [120.0], "ladder", 1)
    assert p3.on_bar_close(b, 29).exit_reason == "time"
    p4 = PositionLogic(1, 100.0, 95.0, [120.0], "ladder", 72)
    assert p4.on_bar_close(b, 30).exit_reason == "stop_close"                 # 종가 94 < 손절 95


def test_split_moves_stop_to_breakeven_after_first_target():
    p = PositionLogic(1, 100.0, 95.0, [110.0, 120.0], "split", 72)
    assert p.on_tp_filled(0) == 100.0 and p.stop == 100.0
    ps = PositionLogic(-1, 100.0, 105.0, [90.0], "split", 72)
    assert ps.on_tp_filled(0) == 100.0 and ps.trailing_rule()                 # 두 번째 존 없음 -> 추적


def test_no_targets_ladder_trails_from_third_bar():
    b = bars_from([(100, 101, 99, 100)] * 25)
    p = PositionLogic(1, 100.0, 95.0, [], "ladder", 72)
    p.on_bar_close(b, 20)
    p.on_bar_close(b, 21)
    assert not p.trailing_active
    p.on_bar_close(b, 22)
    assert p.trailing_active and p.stop > 95.0
