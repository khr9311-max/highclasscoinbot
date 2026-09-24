"""
가격행동 규칙 (Naked Forex, Nekritin & Peters 2012) - 업비트 봇 price_action.py 의 복사본.

이 파일의 '원본 구간' (아래 VENDORED BEGIN ~ VENDORED END) 은 저장소 루트
price_action.py 에서 스크립트로 그대로 복사했다. 규칙·임계값을 바꾸지 않았다.
원본과 결과가 같은지는 tests/test_strategy_parity.py 가 매번 확인한다
(원본 파일이 없는 환경에서는 그 테스트만 건너뛴다 - 이 패키지는 원본 없이 동작한다).

복사한 이유: 바이낸스 봇이 업비트 코드에 import 로 묶이면 업비트 쪽 수정이 바이낸스
실거래 동작을 조용히 바꾼다. 독립 실행을 위해 복사하고, 동일성은 테스트로 지킨다.

원본은 업비트 현물용이라 매수(롱) 방향만 진입 패턴으로 쓴다. 바이낸스 V1 의 숏은
원본 코드를 고치지 않고 '가격 축 반전(mirror)' 으로 만든다 (파일 끝 V1 ADDITIONS):
  o' = -o, h' = -l, l' = -h, c' = -c
반전 봉에서 원본 trendy_kangaroo 가 참이면 원래 봉에서는 정확히 대칭인 약세 추세
캥거루다. ATR·R² 는 반전에 불변이고 기울기는 부호만 바뀐다 (테스트로 확인).

판정은 항상 'i 번째 봉까지 마감된 데이터' 만 본다 (미래 봉 참조 경로 없음).
"""

# ============================ VENDORED BEGIN (price_action.py) ============================
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# 임계값 (출처: 책 해당 장). 바꾸면 validate_naked 의 시행 수에 반영할 것.
# ---------------------------------------------------------------------------
ROOM_TO_LEFT_MIN = 7          # 6·8장: 최소 7봉 동안 그 가격대에서 거래가 없어야
TOP_THIRD = 2.0 / 3.0         # 8장: 캥거루 시가·종가는 봉의 위쪽 1/3
BIG_SHADOW_CLOSE_POS = 0.75   # 6장: 종가가 고가 근처 (위쪽 25%)
BIG_SHADOW_RANGE_LOOKBACK = 5  # 6장: 직전 5봉 중 가장 큰 봉
KANGAROO_GIANT_LOOKBACK = 3   # 8장: 직전 봉들이 캥거루보다 크면 추세 지속
WAMMIE_MIN_GAP = 6            # 7장: 두 터치 사이 최소 6봉
WAMMIE_MAX_GAP = 60
PAUSE_MIN, PAUSE_MAX = 3, 10  # 10장: 추세 중 쉬어가는 3~10봉
ZONE_TOUCH_MIN = 3            # 4장: 여러 번 꺾인 곳만 존 (적게 그릴수록 낫다)

EXIT_ONLY = "exit"
ENTRY = "entry"


# ---------------------------------------------------------------------------
# 봉 데이터
# ---------------------------------------------------------------------------
@dataclass
class Bars:
    """시간순(오래된 것 -> 최신) OHLCV. t 는 봉 시작 시각(epoch 초, UTC)."""

    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    period: int                   # 초 (1시간봉 = 3600)

    def __len__(self) -> int:
        return len(self.c)

    def close_time(self, i: int) -> float:
        return float(self.t[i]) + self.period

    def upto(self, n: int) -> "Bars":
        """앞에서 n 개만 (n 번째는 제외)."""
        return Bars(self.t[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n],
                    self.v[:n], self.period)

    def tail(self, n: int) -> "Bars":
        """뒤에서 n 개만 (라이브는 최근 200봉만 받으므로 백테스트도 맞춘다)."""
        k = max(0, len(self) - n)
        return Bars(self.t[k:], self.o[k:], self.h[k:], self.l[k:], self.c[k:],
                    self.v[k:], self.period)

    def closed_by(self, ts: float) -> "Bars":
        """ts 시각까지 '마감된' 봉만. 상위 시간봉을 하위 시간봉 시점에 맞출 때 쓴다."""
        n = int(np.searchsorted(self.t + self.period, ts, side="right"))
        return self.upto(n)

    @classmethod
    def from_rows(cls, rows: Sequence[Sequence[float]], period: int) -> "Bars":
        """rows = [(t, o, h, l, c, v), ...] 순서 무관. 시각 중복은 뒤의 것이 이긴다."""
        dedup = {}
        for r in rows:
            dedup[float(r[0])] = r
        arr = np.asarray([dedup[k] for k in sorted(dedup)], dtype=float).reshape(-1, 6)
        return cls(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5], period)


def atr(bars: Bars, i: int, n: int = 14) -> float:
    """i 번째 봉까지의 평균 참범위(단순평균)."""
    lo = max(1, i - n + 1)
    if i < 1:
        return float(bars.h[0] - bars.l[0]) if len(bars) else 0.0
    h = bars.h[lo:i + 1]
    l = bars.l[lo:i + 1]
    pc = bars.c[lo - 1:i]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return float(tr.mean()) if tr.size else 0.0


def _rng(bars: Bars, i: int) -> float:
    return float(bars.h[i] - bars.l[i])


def close_pos(bars: Bars, i: int) -> float:
    """봉 안에서 종가 위치. 0 = 저가, 1 = 고가."""
    r = _rng(bars, i)
    return float((bars.c[i] - bars.l[i]) / r) if r > 0 else 0.5


# ---------------------------------------------------------------------------
# 존 (4장)
# ---------------------------------------------------------------------------
@dataclass
class Zone:
    price: float          # 중심
    lo: float             # 밴드 하단 (허용오차 포함)
    hi: float             # 밴드 상단
    touches: int
    extreme: bool = False  # 극단 고점/저점에서 나온 존 (터치 1회라도 인정, 4장)

    def to_dict(self) -> Dict[str, float]:
        return {"price": self.price, "lo": self.lo, "hi": self.hi,
                "touches": self.touches, "extreme": self.extreme}


def find_zones(bars: Bars, pivot_k: int = 3, tol_atr: float = 0.5,
               min_touches: int = ZONE_TOUCH_MIN, min_sep_atr: float = 1.0,
               max_zones: int = 14) -> List[Zone]:
    """
    존 = 가격이 반복해서 꺾인 구간 (4장).

    - 종가로 찾는다. 책이 '선차트(종가 연결)의 꺾임' 을 존 찾는 방법으로
      권하고(4장 'The Line Chart Is Your Friend'), 종가가 세력들이 하루를
      정리하는 가격이라 가장 중요하다고 본다.
    - 존은 점이 아니라 구간이다 ('beer belly'). 0.5 ATR 안의 꺾임은 한 존.
    - 3번 이상 꺾인 곳만 남긴다. 너무 많이 그리면 매일 신호가 나고 대부분
      마이너 존이라 진다 (4장 'Five Tips' 두 번째).
    - 극단 고점/저점은 1번만 닿아도 존이다 (4장 GBP/USD 1.6291 예).
    - 1 ATR 보다 가까운 존은 터치가 많은 쪽만 남긴다.

    bars 는 상위 시간봉(기본 4시간봉)이어야 한다 - 한 단계 위 시간봉에서
    그려야 중요한 존만 남는다 (4장 'Use a Higher Timeframe Chart').
    """
    n = len(bars)
    if n < 2 * pivot_k + 5:
        return []
    a = atr(bars, n - 1)
    if a <= 0:
        return []
    c = bars.c

    piv_idx, piv_px = [], []
    for i in range(pivot_k, n - pivot_k):
        seg = c[i - pivot_k:i + pivot_k + 1]
        if c[i] == seg.max() or c[i] == seg.min():
            piv_idx.append(i)
            piv_px.append(float(c[i]))

    tol = tol_atr * a
    order = np.argsort(piv_px)
    clusters: List[List[int]] = []
    for k in order:
        if clusters and piv_px[k] - piv_px[clusters[-1][0]] <= tol:
            clusters[-1].append(k)
        else:
            clusters.append([k])

    zones: List[Zone] = []
    for cl in clusters:
        idxs = sorted(piv_idx[k] for k in cl)
        # 같은 꺾임이 연달아 잡힌 것(평평한 고점)은 1회로 센다
        touches = 1
        for p, q in zip(idxs, idxs[1:]):
            if q - p >= pivot_k:
                touches += 1
        if touches < min_touches:
            continue
        pxs = [piv_px[k] for k in cl]
        zones.append(Zone(float(np.median(pxs)), min(pxs), max(pxs), touches))

    # 극단값 (심지 포함). 여기서 크게 되돌린 자리는 나중에 다시 온다.
    for px in (float(bars.h.max()), float(bars.l.min())):
        zones.append(Zone(px, px, px, 1, extreme=True))

    zones.sort(key=lambda z: (-z.touches, z.extreme))
    kept: List[Zone] = []
    for z in zones:
        if all(abs(z.price - k.price) >= min_sep_atr * a for k in kept):
            kept.append(z)
        if len(kept) >= max_zones:
            break

    pad = 0.25 * a
    out = [Zone(z.price, min(z.lo, z.price) - pad, max(z.hi, z.price) + pad,
                z.touches, z.extreme) for z in kept]
    out.sort(key=lambda z: z.price)
    return out


def support_touched(zones: List[Zone], low: float, close: float) -> Optional[Zone]:
    """
    저가가 지지 존에 닿고 종가는 존 아래로 마감하지 않았다.
    캥거루 꼬리가 존을 뚫었다가 반대편에서 마감하는 전형(8장)을 포함한다.
    여러 개면 저가에 가장 가까운 존.
    """
    hit = [z for z in zones if low <= z.hi and close >= z.lo]
    if not hit:
        return None
    return min(hit, key=lambda z: abs(z.price - low))


def resistance_touched(zones: List[Zone], high: float, close: float) -> Optional[Zone]:
    hit = [z for z in zones if high >= z.lo and close <= z.hi]
    if not hit:
        return None
    return min(hit, key=lambda z: abs(z.price - high))


def targets_above(zones: List[Zone], price: float, min_dist: float) -> List[float]:
    """진입가 위의 존들. 목표가는 존 하단 - 존에 '거의' 닿고 되밀리는 경우가 많다(11장)."""
    out = sorted(z.lo for z in zones if z.lo > price + min_dist)
    return out


# ---------------------------------------------------------------------------
# 보조 판정
# ---------------------------------------------------------------------------
def room_to_left_low(bars: Bars, i: int, level: float) -> int:
    """
    '왼쪽 여백' (6·8장): i 이전에 저가가 level 보다 높았던 연속 봉 수.
    시장이 오랫동안 가보지 않은 가격에서 찍힌 반전일수록 큰 전환점이다.
    """
    k = 0
    for j in range(i - 1, -1, -1):
        if bars.l[j] > level:
            k += 1
        else:
            break
    return k


def room_to_left_high(bars: Bars, i: int, level: float) -> int:
    k = 0
    for j in range(i - 1, -1, -1):
        if bars.h[j] < level:
            k += 1
        else:
            break
    return k


def trend_strength(bars: Bars, end: int, lookback: int = 40) -> Tuple[float, float]:
    """
    (ATR 단위 추세 크기, R²). 10장의 '8살 컨설턴트' 를 숫자로 옮긴 것:
    선형회귀 기울기 x 구간 길이 / ATR 이 크고, 직선에 잘 붙어 있으면(R²) 추세.
    """
    lo = end - lookback + 1
    if lo < 0:
        return 0.0, 0.0
    y = bars.c[lo:end + 1]
    x = np.arange(len(y), dtype=float)
    if np.ptp(y) <= 0:
        return 0.0, 0.0
    slope, icpt = np.polyfit(x, y, 1)
    fit = slope * x + icpt
    ss_res = float(((y - fit) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    a = atr(bars, end)
    return (float(slope * lookback / a) if a > 0 else 0.0), r2


# ---------------------------------------------------------------------------
# 신호
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    pattern: str
    direction: int                 # +1 매수 / -1 매도(청산 전용)
    kind: str                      # ENTRY / EXIT_ONLY
    bar_index: int
    bar_time: float                # 신호 봉 시작 시각
    close_time: float              # 신호 봉 마감 시각 (= 판정 시각)
    entry: float                   # 매수스톱 가격
    stop: float                    # 손절
    targets: List[float] = field(default_factory=list)
    soft_exit: Optional[float] = None   # 종가가 이 아래로 마감하면 청산 (라스트 키스 박스 상단)
    zone: Optional[Dict[str, float]] = None
    features: Dict[str, float] = field(default_factory=dict)

    @property
    def risk_pct(self) -> float:
        return (self.entry - self.stop) / self.entry if self.entry > 0 else 0.0

    def to_dict(self) -> Dict:
        return {
            "pattern": self.pattern, "direction": self.direction, "kind": self.kind,
            "bar_time": self.bar_time, "close_time": self.close_time,
            "entry": self.entry, "stop": self.stop, "targets": list(self.targets),
            "soft_exit": self.soft_exit, "zone": self.zone,
            "features": dict(self.features),
        }


def _base_features(bars: Bars, i: int, a: float) -> Dict[str, float]:
    r = _rng(bars, i)
    v_avg = float(bars.v[max(0, i - 20):i].mean()) if i > 0 else 0.0
    slope, r2 = trend_strength(bars, i)
    return {
        "range_atr": r / a if a > 0 else 0.0,
        "close_pos": close_pos(bars, i),
        "vol_ratio": float(bars.v[i] / v_avg) if v_avg > 0 else 1.0,
        "trend_atr": slope,
        "trend_r2": r2,
    }


def _finish(sig: Signal, zones: List[Zone], a: float, min_rr: float) -> Signal:
    """목표가 = 위 존들. 첫 존이 손절폭보다 가까우면 다음 존으로 (7장 물라 예)."""
    risk = sig.entry - sig.stop
    tg = targets_above(zones, sig.entry, 0.3 * a)
    while tg and risk > 0 and (tg[0] - sig.entry) < min_rr * risk:
        tg = tg[1:]
    sig.targets = tg[:3]
    sig.features["rr"] = ((tg[0] - sig.entry) / risk) if (tg and risk > 0) else 0.0
    sig.features["risk_pct"] = sig.risk_pct
    return sig


def kangaroo_tail(bars: Bars, i: int, zones: List[Zone], bullish: bool = True
                  ) -> Optional[Dict[str, float]]:
    """
    캥거루 꼬리 (8장). 한 봉짜리.
      - 시가·종가 둘 다 봉의 위쪽 1/3 (약세는 아래쪽 1/3)
      - 시가·종가가 직전 봉 범위 안 (폭주장 제외)
      - 봉이 직전 3봉보다 크다 (거대한 봉 뒤의 꼬리는 잠깐 쉬는 것뿐)
      - 1 ATR 이상 (꼬리가 짧으면 존을 다시 시험하러 온다)
      - 존에서 찍힌다 + 왼쪽 여백 7봉 이상
    반환: 공통 특징 (조건 불충족이면 None).
    """
    if i < max(KANGAROO_GIANT_LOOKBACK, 1) + 1:
        return None
    o, h, l, c = bars.o[i], bars.h[i], bars.l[i], bars.c[i]
    r = h - l
    a = atr(bars, i)
    if r <= 0 or a <= 0 or r < a:
        return None
    ph, pl = bars.h[i - 1], bars.l[i - 1]
    if not (pl <= o <= ph and pl <= c <= ph):
        return None
    prior_max = float(np.max(bars.h[i - KANGAROO_GIANT_LOOKBACK:i] - bars.l[i - KANGAROO_GIANT_LOOKBACK:i]))
    if r < prior_max:
        return None
    if bullish:
        if min(o, c) < l + TOP_THIRD * r:
            return None
        z = support_touched(zones, l, c)
        room = room_to_left_low(bars, i, l)
        tail = (min(o, c) - l) / r
    else:
        if max(o, c) > h - TOP_THIRD * r:
            return None
        z = resistance_touched(zones, h, c)
        room = room_to_left_high(bars, i, h)
        tail = (h - max(o, c)) / r
    if z is None or room < ROOM_TO_LEFT_MIN:
        return None
    return {"zone": z, "room": room, "tail": tail, "atr": a}


def big_shadow(bars: Bars, i: int, zones: List[Zone], bullish: bool = True
               ) -> Optional[Dict[str, float]]:
    """
    빅 섀도 (6장). 두 봉짜리, 두 번째 봉이 첫 봉을 고가·저가 모두 감싼다.
      - 종가가 고가 근처 (위쪽 25%). 중간 마감은 실패 확률이 높다
      - 직전 5봉 중 가장 큰 봉
      - 존에서 + 왼쪽 여백 7봉 이상 (두 봉이 함께 차지한 공간 기준)
    """
    if i < BIG_SHADOW_RANGE_LOOKBACK + 1:
        return None
    o, h, l, c = bars.o[i], bars.h[i], bars.l[i], bars.c[i]
    if not (h > bars.h[i - 1] and l < bars.l[i - 1]):
        return None
    r = h - l
    a = atr(bars, i)
    if r <= 0 or a <= 0:
        return None
    lb = BIG_SHADOW_RANGE_LOOKBACK
    if r < float(np.max(bars.h[i - lb:i] - bars.l[i - lb:i])):
        return None
    cp = (c - l) / r
    if bullish:
        if cp < BIG_SHADOW_CLOSE_POS or c <= o:
            return None
        z = support_touched(zones, l, c)
        room = room_to_left_low(bars, i - 1, l)
    else:
        if cp > 1.0 - BIG_SHADOW_CLOSE_POS or c >= o:
            return None
        z = resistance_touched(zones, h, c)
        room = room_to_left_high(bars, i - 1, h)
    if z is None or room < ROOM_TO_LEFT_MIN:
        return None
    return {"zone": z, "room": room, "atr": a}


def wammie(bars: Bars, i: int, zones: List[Zone], bullish: bool = True
           ) -> Optional[Dict[str, float]]:
    """
    와미 / 물라 (7장). 이중바닥(이중천장)의 특수형.
      - 같은 존을 두 번 닿는다. 두 번째 저점이 더 높다 (물라는 두 번째 고점이 더 낮다)
      - 두 터치 사이 6봉 이상, 그 사이에 존에서 1 ATR 이상 떨어졌다 온다
        (빠르게 여러 번 두드리면 오히려 돌파 전조 - 7.12 그림)
      - 두 번째 터치 뒤 첫 강한 양봉(종가 위쪽 40% 안)에서 신호
      - 손절은 첫 번째(더 낮은) 터치 아래 - 세 번째 터치를 견딘다 (7.10 그림)
    """
    if i < WAMMIE_MIN_GAP + 3:
        return None
    a = atr(bars, i)
    if a <= 0:
        return None
    c, o = bars.c[i], bars.o[i]
    strong = (c > o and close_pos(bars, i) >= 0.6) if bullish else (c < o and close_pos(bars, i) <= 0.4)
    if not strong:
        return None

    for z in zones:
        if bullish:
            touching = lambda j: bars.l[j] <= z.hi and bars.c[j] >= z.lo
        else:
            touching = lambda j: bars.h[j] >= z.lo and bars.c[j] <= z.hi
        # 두 번째 터치: 신호봉 포함 최근 3봉 안
        s = next((j for j in range(i, max(i - 3, 0) - 1, -1) if touching(j)), None)
        if s is None:
            continue
        # 신호봉이 두 번째 터치 뒤 '첫' 강한 봉인지 (같은 패턴 중복 발동 방지)
        dup = False
        for j in range(s, i):
            cj, oj = bars.c[j], bars.o[j]
            if bullish and cj > oj and close_pos(bars, j) >= 0.6:
                dup = True
            if not bullish and cj < oj and close_pos(bars, j) <= 0.4:
                dup = True
        if dup:
            continue
        # 두 번째 터치 무리의 극값
        s0 = s
        while s0 - 1 >= 0 and touching(s0 - 1) and s - s0 < 3:
            s0 -= 1
        second = float(bars.l[s0:s + 1].min()) if bullish else float(bars.h[s0:s + 1].max())

        # 첫 번째 터치: 두 번째 터치 무리보다 6봉 이상 앞
        f_end = s0 - WAMMIE_MIN_GAP
        f_start = max(0, s0 - WAMMIE_MAX_GAP)
        first_idx = None
        for j in range(f_end, f_start - 1, -1):
            if touching(j):
                first_idx = j
                break
        if first_idx is None:
            continue
        # 사이 구간에는 존 터치가 없어야 하고, 1 ATR 이상 떨어졌어야 한다
        between = range(first_idx + 1, s0)
        if any(touching(j) for j in between if j - first_idx > 2):
            continue
        f0, f1 = max(0, first_idx - 2), first_idx + 1
        if bullish:
            first = float(bars.l[f0:f1].min())
            away = float(bars.h[first_idx + 1:s0].max()) if s0 > first_idx + 1 else 0.0
            if second <= first or away < z.hi + a:
                continue
        else:
            first = float(bars.h[f0:f1].max())
            away = float(bars.l[first_idx + 1:s0].min()) if s0 > first_idx + 1 else 1e18
            if second >= first or away > z.lo - a:
                continue
        return {"zone": z, "first": first, "second": second,
                "gap": s0 - first_idx, "atr": a}
    return None


def last_kiss(bars: Bars, i: int, max_after: int = 12) -> Optional[Dict[str, float]]:
    """
    라스트 키스 (5장). 매수 방향만 (하방 돌파는 현물로 못 탄다).
      - 박스: 20~60봉 횡보, 높이 1.5~8 ATR, 상단·하단 각각 2회 이상 터치
      - 돌파: 종가가 상단 위로 (첫 이탈)
      - 되돌림: 신호봉 저가가 상단 가장자리까지 내려오고, 종가는 상단 위, 양봉
      - 돌파 이후 종가가 박스 안으로 돌아온 적 없어야 (그건 가짜 돌파)
      - 손절: 박스 중간(비상), 종가가 박스 안으로 돌아오면 즉시 청산
    """
    a = atr(bars, i)
    if a <= 0 or i < 30:
        return None
    if not (bars.c[i] > bars.o[i]):
        return None

    for k in range(i - 1, max(i - max_after, 1) - 1, -1):
        for L in (60, 40, 30, 20):
            lo = k - L
            if lo < 1:
                continue
            top = float(bars.h[lo:k].max())
            bot = float(bars.l[lo:k].min())
            height = top - bot
            ak = atr(bars, k - 1)
            if ak <= 0 or not (1.5 * ak <= height <= 8.0 * ak):
                continue
            # 돌파봉: 이 봉이 처음으로 종가가 상단 위
            if not (bars.c[k] > top and bars.c[k - 1] <= top):
                continue
            tol = 0.3 * ak
            if _count_touches(bars.h[lo:k], top - tol, above=True) < 2:
                continue
            if _count_touches(bars.l[lo:k], bot + tol, above=False) < 2:
                continue
            # 돌파 뒤 박스 안 마감 없음
            if any(bars.c[j] < top for j in range(k + 1, i + 1)):
                break
            # 되돌림: 신호봉이 상단에 닿고, 그 전(돌파~신호) 에는 닿지 않았다
            kiss = lambda j: bars.l[j] <= top + tol
            if not kiss(i):
                break
            if any(kiss(j) for j in range(k + 1, i)):
                break
            return {"top": top, "bottom": bot, "box_len": L,
                    "breakout_age": i - k, "atr": a}
    return None


def _count_touches(xs: np.ndarray, level: float, above: bool, sep: int = 3) -> int:
    idx = np.nonzero(xs >= level if above else xs <= level)[0]
    if idx.size == 0:
        return 0
    n, last = 1, idx[0]
    for j in idx[1:]:
        if j - last >= sep:
            n += 1
        last = j
    return n


def trendy_kangaroo(bars: Bars, i: int) -> Optional[Dict[str, float]]:
    """
    추세 캥거루 (10장). 매수 방향만.
      - 상승 추세: 직전 40봉 회귀 기울기 3 ATR 이상, R² 0.5 이상
      - 쉬어가기: 신호 직전 3~10봉이 2 ATR 안에서 횡보
      - 캥거루 모양(위쪽 1/3, 직전 봉 범위 안) + 꼬리가 쉬어가기 저점 아래로 튀어나옴
      - 큰 조정 뒤는 제외: 쉬어가기 고점이 추세 고점에서 1.5 ATR 이내
    존은 필수가 아니다 (책도 쉬어가기가 만든 마이너 존이면 된다고 한다).
    """
    if i < 50:
        return None
    o, h, l, c = bars.o[i], bars.h[i], bars.l[i], bars.c[i]
    r = h - l
    a = atr(bars, i)
    if r <= 0 or a <= 0:
        return None
    ph, pl = bars.h[i - 1], bars.l[i - 1]
    if not (pl <= o <= ph and pl <= c <= ph):
        return None
    if min(o, c) < l + TOP_THIRD * r:
        return None

    pause = 0
    for p in range(PAUSE_MAX, PAUSE_MIN - 1, -1):
        seg_h, seg_l = bars.h[i - p:i], bars.l[i - p:i]
        if float(seg_h.max() - seg_l.min()) <= 2.0 * a:
            pause = p
            break
    if not pause:
        return None
    p_lo = float(bars.l[i - pause:i].min())
    p_hi = float(bars.h[i - pause:i].max())
    if l >= p_lo - 0.1 * a:          # 꼬리가 쉬어가기 구간 밖으로 나와야
        return None

    slope, r2 = trend_strength(bars, i - pause, lookback=40)
    if slope < 3.0 or r2 < 0.5:
        return None
    trend_high = float(bars.h[i - pause - 40:i].max())
    if p_hi < trend_high - 1.5 * a:
        return None
    return {"pause": pause, "pause_lo": p_lo, "atr": a}


# ---------------------------------------------------------------------------
# 한 봉 판정 (라이브·백테스트 공용)
# ---------------------------------------------------------------------------
ENTRY_PATTERNS = ("kangaroo", "big_shadow", "wammie", "last_kiss", "trendy_kangaroo")


def evaluate_bar(bars: Bars, i: int, zones: List[Zone],
                 patterns: Sequence[str] = ENTRY_PATTERNS,
                 min_rr: float = 1.0, min_risk_pct: float = 0.0024,
                 max_risk_pct: float = 0.08) -> List[Signal]:
    """
    i 번째(마감된) 봉에서 나온 신호 목록.
    매수 진입 신호는 손절폭이 비용 대비 너무 좁거나(왕복비용 x2 미만) 너무
    넓으면(8% 초과) 버린다 - 좁으면 수수료가 손익을 지배하고, 넓으면 한 번에
    너무 많이 잃는다.
    """
    out: List[Signal] = []
    a = atr(bars, i)
    if a <= 0:
        return out
    buf = 0.05 * a                 # 책의 '몇 핍'
    bt, ct = float(bars.t[i]), bars.close_time(i)
    h, l = float(bars.h[i]), float(bars.l[i])

    def entry_sig(name: str, stop: float, zone: Optional[Zone], extra: Dict[str, float],
                  soft: Optional[float] = None):
        sig = Signal(name, +1, ENTRY, i, bt, ct, h + buf, stop - buf,
                     soft_exit=soft, zone=zone.to_dict() if zone else None,
                     features={**_base_features(bars, i, a), **extra})
        if zone is not None:
            sig.features["zone_touches"] = float(zone.touches)
        _finish(sig, zones, a, min_rr)
        if min_risk_pct <= sig.risk_pct <= max_risk_pct:
            out.append(sig)

    if "kangaroo" in patterns:
        k = kangaroo_tail(bars, i, zones, bullish=True)
        if k:
            entry_sig("kangaroo", l, k["zone"], {"room": k["room"], "tail": k["tail"]})
    if "big_shadow" in patterns:
        b = big_shadow(bars, i, zones, bullish=True)
        if b:
            entry_sig("big_shadow", l, b["zone"], {"room": b["room"]})
    if "wammie" in patterns:
        w = wammie(bars, i, zones, bullish=True)
        if w:
            entry_sig("wammie", w["first"], w["zone"], {"gap": w["gap"]})
    if "last_kiss" in patterns:
        lk = last_kiss(bars, i)
        if lk:
            mid = (lk["top"] + lk["bottom"]) / 2.0
            entry_sig("last_kiss", mid + buf, None,
                      {"box_len": lk["box_len"], "breakout_age": lk["breakout_age"]},
                      soft=lk["top"])
    if "trendy_kangaroo" in patterns:
        tk = trendy_kangaroo(bars, i)
        if tk:
            entry_sig("trendy_kangaroo", l, None, {"pause": tk["pause"]})

    # 청산 전용: 저항 존의 약세 반전 (13장)
    for name, fn in (("bear_kangaroo", kangaroo_tail), ("bear_big_shadow", big_shadow),
                     ("moolah", wammie)):
        res = fn(bars, i, zones, bullish=False)
        if res:
            out.append(Signal(name, -1, EXIT_ONLY, i, bt, ct, l - buf, h + buf,
                              zone=res["zone"].to_dict(),
                              features=_base_features(bars, i, a)))
    return out

# ============================= VENDORED END (price_action.py) =============================


# ============================== V1 ADDITIONS (바이낸스 전용) ==============================
# 원본 함수를 고치지 않고 숏 방향을 만든다. 아래는 원본에 없던 보조 함수들이다.

def mirror_bars(bars: Bars) -> Bars:
    """가격 축 반전. 약세 패턴 = 반전 봉에서의 강세 패턴."""
    return Bars(bars.t, -bars.o, -bars.l, -bars.h, -bars.c, bars.v, bars.period)


def targets_below(zones: List[Zone], price: float, min_dist: float) -> List[float]:
    """
    targets_above 의 대칭. 진입가 아래의 존들, 가까운 것부터.
    목표가는 존 '상단' - 롱이 존 하단에서 되밀리듯 숏은 존 상단에서 되밀린다 (11장의 대칭).
    """
    return sorted((z.hi for z in zones if z.hi < price - min_dist), reverse=True)


def trendy_kangaroo_short(mirrored: Bars, i: int) -> Optional[Dict[str, float]]:
    """
    약세 추세 캥거루 = 반전 봉에서의 원본 trendy_kangaroo.
    반환값 pause_lo 는 반전 공간 값이므로 원래 공간의 '쉬어가기 고점' 으로 되돌린다.
    """
    tk = trendy_kangaroo(mirrored, i)
    if tk is None:
        return None
    return {"pause": tk["pause"], "pause_hi": -tk["pause_lo"], "atr": tk["atr"]}
