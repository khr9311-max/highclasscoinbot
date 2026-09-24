"""
복사한 가격행동 코드가 업비트 원본과 같은 결과를 내는지 확인한다.
원본(저장소 루트 price_action.py)이 없으면 건너뛴다 - 바이낸스 패키지는 원본 없이 돈다.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from binance_coinm_v1.strategy import price_action as vend

from .synth import aggregate, random_walk

ORIG_PATH = Path(__file__).resolve().parents[2] / "price_action.py"


def load_original(path, name):
    """원본 모듈을 읽기만 한다 - 업비트 프로젝트 __pycache__ 에 바이트코드를 쓰지 않는다."""
    import sys
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = prev


@pytest.fixture(scope="module")
def orig():
    if not ORIG_PATH.exists():
        pytest.skip("원본 price_action.py 없음 (독립 배포 환경)")
    return load_original(ORIG_PATH, "upbit_price_action_original")


def _to(mod, b):
    return mod.Bars(b.t, b.o, b.h, b.l, b.c, b.v, b.period)


def _zt(zs):
    return [(round(z.price, 8), round(z.lo, 8), round(z.hi, 8), z.touches, z.extreme) for z in zs]


def test_constants_identical(orig):
    for name in ("ROOM_TO_LEFT_MIN", "TOP_THIRD", "BIG_SHADOW_CLOSE_POS",
                 "BIG_SHADOW_RANGE_LOOKBACK", "KANGAROO_GIANT_LOOKBACK", "WAMMIE_MIN_GAP",
                 "WAMMIE_MAX_GAP", "PAUSE_MIN", "PAUSE_MAX", "ZONE_TOUCH_MIN", "ENTRY_PATTERNS"):
        assert getattr(vend, name) == getattr(orig, name), name


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_vendored_logic_matches_original(orig, seed):
    ltf = random_walk(1400, seed=seed)
    htf = aggregate(ltf, 4)
    o_ltf, o_htf = _to(orig, ltf), _to(orig, htf)
    n_sig = 0
    for i in range(80, len(ltf)):
        ts = ltf.close_time(i)
        zv = vend.find_zones(htf.closed_by(ts).tail(200))
        zo = orig.find_zones(o_htf.closed_by(ts).tail(200))
        assert _zt(zv) == _zt(zo)
        assert vend.atr(ltf, i) == orig.atr(o_ltf, i)
        assert (vend.trendy_kangaroo(ltf, i) or {}) == (orig.trendy_kangaroo(o_ltf, i) or {})
        sv = [s.to_dict() for s in vend.evaluate_bar(ltf, i, zv)]
        so = [s.to_dict() for s in orig.evaluate_bar(o_ltf, i, zo)]
        # Zone 객체는 모듈이 달라 dict 로 비교
        assert sv == so
        n_sig += len(sv)
    assert n_sig > 0          # 비교가 실제 신호를 포함했는지
