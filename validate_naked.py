"""
가격행동 전략 검증.

    python validate_naked.py backtest [--days 180] [--alts 20] [--tickers KRW-BTC,...]
    python validate_naked.py report            # 운영 중 쌓인 종이 매매로 재평가

backtest 는 업비트 과거 캔들(공개 API)로 라이브와 같은 코드(PaperBook)를
봉 단위로 돌린다. 판정은 항상 그 시점까지 마감된 봉만 본다.

결과는 state/naked_validation.json 에 쓰고, 실주문 모드에서 main.py 가 이
파일을 읽어 통과(passed)일 때만 가격행동 실주문을 켠다.

봇은 이 과정을 스스로 돈다 (auto_validate): 시작할 때와 하루에 한 번, 표본 외
종이 매매를 합쳐 재평가하고, 백테스트가 NAKED_BACKTEST_REFRESH_DAYS 보다
오래됐으면 새로 돌린다. 손으로 실행하는 건 확인용이다.

통과 기준 (라이브 패턴 NAKED_LIVE_PATTERNS x 청산 NAKED_EXIT_MODE 합산):
  - 체결된 거래 100건 이상
  - 비용(왕복 0.12%) 차감 평균 순수익 > 0
  - DSR >= 0.95  (시행 수 = 비교한 '청산방식 x 패턴조합' 전부. 라이브 설정을
                  백테스트를 보고 골랐다면 그 선택 과정이 여기서 벌점으로 들어간다)
  - PBO <= 0.5   (그 조합들로 CSCV)
  - 표본 외 종이 매매 30건 이상, 평균 순수익 > 0
    라이브 설정은 백테스트를 보고 골랐으므로, 그 뒤에 쌓인 종이 매매만이
    진짜 표본 외 증거다. 이게 없으면 백테스트가 아무리 좋아도 통과시키지 않는다.

report 는 백테스트 거래 + 운영 종이 매매를 합쳐 다시 평가한다. 엣지가 진짜면
표본이 쌓일수록 DSR 이 올라간다.

한계 (리포트에도 적는다):
  - 알트 유니버스는 '지금' 거래대금 상위라 생존자 편향이 있다. 과거에 상장폐지
    됐거나 거래가 말라버린 종목은 빠져 있다.
  - 1시간봉 안의 경로를 모르므로 체결을 보수적으로 가정했다
    (naked_strategy 상단 주석). 실제보다 약간 나쁘게 나오는 쪽이다.
  - 시장가 슬리피지는 왕복비용 0.12% 안에만 들어 있다. 호가가 얇은 알트는 더 크다.
"""

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from price_action import ENTRY_PATTERNS, find_zones
from naked_strategy import EXIT_MODES, PaperBook

logger = logging.getLogger(__name__)

MIN_TRADES = 100
MIN_DSR = 0.95
MAX_PBO = 0.5
MIN_PAPER = 30


# ---------------------------------------------------------------------------
# 백테스트
# ---------------------------------------------------------------------------
def backtest_ticker(ticker: str, ltf, htf, is_alt: bool, patterns: Sequence[str],
                    min_rr: float, valid_bars: int, max_hold: int,
                    zone_bars: int = 200, warmup: int = 60) -> List[Dict[str, Any]]:
    book = PaperBook(EXIT_MODES, patterns, min_rr, valid_bars, max_hold)
    out: List[Dict[str, Any]] = []
    zkey, zones = None, []
    for i in range(warmup, len(ltf)):
        h = htf.closed_by(ltf.close_time(i)).tail(zone_bars)
        if len(h) < 60:
            continue
        key = (len(h), float(h.t[-1]))
        if key != zkey:
            zones, zkey = find_zones(h), key
        _, finished = book.process_bar(ticker, ltf, i, zones, is_alt)
        for tr in finished:
            r = tr.result()
            r["source"] = "backtest"
            out.append(r)
    return out


async def run_backtest(tickers: Sequence[str], alt_set: set, days: int, cfg,
                       per_second: Optional[int] = None, verbose: bool = True) -> List[Dict]:
    """
    봇 안에서도 돌 수 있게 만든다:
      - 계산(종목당 수 초)은 스레드에서 돌려 이벤트 루프(틱·주문)를 막지 않는다
      - 캔들 요청은 라이브 스캔과 합쳐 업비트 한도(초당 10회)를 넘지 않게 느리게
    """
    from candle_feed import CandleFeed
    feed = CandleFeed(per_second=per_second or CandleFeed.BACKTEST_PER_SECOND)
    trades: List[Dict] = []
    say = print if verbose else (lambda *a, **k: None)
    try:
        for t in tickers:
            t0 = time.time()
            ltf = await feed.fetch_history(t, cfg.NAKED_TF_MIN, days * 24 + 100)
            htf = await feed.fetch_history(t, cfg.NAKED_ZONE_TF_MIN, days * 6 + 260)
            if ltf is None or htf is None or len(ltf) < 200 or len(htf) < 80:
                say(f"  {t:<12} 데이터 부족 - 건너뜀")
                continue
            res = await asyncio.to_thread(
                backtest_ticker, t, ltf, htf, t in alt_set, cfg.NAKED_PATTERNS,
                cfg.NAKED_MIN_RR, cfg.NAKED_ENTRY_VALID_BARS,
                cfg.NAKED_MAX_HOLD_BARS, cfg.NAKED_ZONE_BARS)
            trades.extend(res)
            n_sig = len({(r["signal_ts"], r["pattern"]) for r in res})
            say(f"  {t:<12} 1h {len(ltf):>5}봉 · 신호 {n_sig:>3} · {time.time()-t0:4.1f}s")
    finally:
        await feed.close()
    return trades


def backtest_path(state_dir: str) -> str:
    return os.path.join(state_dir, "naked", "backtest_trades.jsonl")


def save_backtest(state_dir: str, trades: Sequence[Dict], meta: Dict[str, Any]):
    os.makedirs(os.path.join(state_dir, "naked"), exist_ok=True)
    path = backtest_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in trades:
            f.write(json.dumps(r, ensure_ascii=False, default=float) + "\n")
    os.replace(tmp, path)
    with open(path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({**meta, "generated_at": time.time()}, f, ensure_ascii=False, indent=2)


def backtest_params(cfg) -> Dict[str, Any]:
    """백테스트 결과를 바꾸는 설정 전부. 하나라도 바뀌면 백테스트를 다시 돌린다."""
    return {"patterns": list(cfg.NAKED_PATTERNS), "exit_modes": list(EXIT_MODES),
            "min_rr": cfg.NAKED_MIN_RR, "valid_bars": cfg.NAKED_ENTRY_VALID_BARS,
            "max_hold": cfg.NAKED_MAX_HOLD_BARS, "tf": cfg.NAKED_TF_MIN,
            "zone_tf": cfg.NAKED_ZONE_TF_MIN, "zone_bars": cfg.NAKED_ZONE_BARS}


def backtest_meta(state_dir: str) -> Dict[str, Any]:
    try:
        with open(backtest_path(state_dir) + ".meta.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def reevaluate(state_dir: str, cfg) -> Optional[Dict[str, Any]]:
    """백테스트 거래 + 표본 외 종이 매매로 재평가하고 리포트를 쓴다 (네트워크 없음)."""
    trades = load_backtest(state_dir)
    if not trades:
        return None
    paper = load_paper(state_dir)
    seen = {(t["ticker"], t["signal_ts"], t["pattern"], t["variant"]) for t in trades}
    merged = trades + [t for t in paper
                       if (t["ticker"], t["signal_ts"], t["pattern"], t["variant"]) not in seen]
    rep = evaluate(merged, cfg.NAKED_PATTERNS, cfg.NAKED_EXIT_MODE, paper, cfg.NAKED_LIVE_PATTERNS)
    bm = backtest_meta(state_dir)
    write_report(state_dir, rep, {
        "source": "backtest+paper", "backtest": bm,
        "survivorship_bias_note": "알트는 백테스트 시점 거래대금 상위 기준이라 생존자 편향이 있음"})
    return rep


async def auto_validate(state_dir: str, cfg, refresh_days: float, days: int) -> Dict[str, Any]:
    """
    봇이 매일 부르는 검증. 백테스트가 없거나 refresh_days 보다 오래됐으면 새로
    돌리고(약 4분, 백그라운드), 그 다음 표본 외 종이 매매까지 합쳐 재평가한다.
    이게 없으면 게이트가 요구하는 '표본 외 종이매매' 조건을 사람이 직접
    report 를 돌려야만 반영할 수 있었다.
    """
    bm = backtest_meta(state_dir)
    stale = (not os.path.exists(backtest_path(state_dir))
             or time.time() - float(bm.get("generated_at", 0)) > refresh_days * 86400
             or bm.get("params") != backtest_params(cfg))
    if stale:
        core = list(cfg.TARGET_TICKERS)
        alts = await _universe_now(cfg, cfg.NAKED_ALT_TOP_N) if cfg.NAKED_ALTS_ENABLED else []
        trades = await run_backtest(core + alts, set(alts), days, cfg, verbose=False)
        if trades:
            save_backtest(state_dir, trades, {"days": days, "tickers": core + alts, "alts": alts,
                                              "params": backtest_params(cfg)})
    rep = await asyncio.to_thread(reevaluate, state_dir, cfg)
    return {"backtest_refreshed": stale, "report": rep}


# ---------------------------------------------------------------------------
# 지표
# ---------------------------------------------------------------------------
def summarize(rets: Sequence[float], rs: Sequence[Optional[float]] = ()) -> Dict[str, Any]:
    r = np.asarray(rets, dtype=float)
    if r.size == 0:
        return {"n": 0}
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    sd = r.std(ddof=1) if r.size > 1 else 0.0
    rr = np.asarray([x for x in rs if x is not None], dtype=float)
    return {
        "n": int(r.size),
        "win_rate": round(float((r > 0).mean()), 4),
        "mean_net": round(float(r.mean()), 6),
        "median_net": round(float(np.median(r)), 6),
        "total_net": round(float(r.sum()), 4),
        "profit_factor": round(float(pos / neg), 3) if neg > 0 else None,
        "sharpe_per_trade": round(float(r.mean() / sd), 4) if sd > 0 else None,
        "mean_r": round(float(rr.mean()), 3) if rr.size else None,
    }


def _filled(trades: Sequence[Dict]) -> List[Dict]:
    return [t for t in trades if t.get("filled") and t.get("status") == "closed"]


def non_overlap(trades: Sequence[Dict]) -> List[Dict]:
    """
    라이브 규칙 '종목당 거래 하나' 를 적용한다. 종이 장부는 패턴별로 따로
    굴리므로(naked_strategy.PaperBook), 여러 패턴을 함께 쓰는 전략을 평가할
    때는 여기서 겹치는 거래를 뺀다. 매수스톱 대기 중(체결 전 취소된 것 포함)도
    점유로 본다. 같은 봉 신호끼리는 ENTRY_PATTERNS 순서가 우선.
    """
    order = {p: k for k, p in enumerate(ENTRY_PATTERNS)}
    out, busy_until = [], {}
    for t in sorted(trades, key=lambda r: (r["signal_ts"], order.get(r["pattern"], 99))):
        if t["signal_ts"] < busy_until.get(t["ticker"], float("-inf")):
            continue
        out.append(t)
        busy_until[t["ticker"]] = t.get("exit_ts") or float("inf")
    return out


def select(trades: Sequence[Dict], variant: str, patterns: Sequence[str]) -> List[Dict]:
    """한 전략(청산방식 x 패턴 집합)이 실제로 냈을 거래 (체결·종료된 것만)."""
    pool = [t for t in trades if t["variant"] == variant and t["pattern"] in patterns]
    if len(set(patterns)) > 1:
        pool = non_overlap(pool)
    return _filled(pool)


def strategy_grid(trades: Sequence[Dict], patterns: Sequence[str]) -> Dict[str, List[Dict]]:
    """비교 대상 전략 = 청산방식 x (패턴 하나씩 + 전체). 이게 DSR/PBO 의 시행 집합이다."""
    subsets = [(p,) for p in patterns] + [tuple(patterns)]
    grid = {}
    for v in EXIT_MODES:
        for sub in subsets:
            name = f"{v}|{'+'.join(sub) if len(sub) > 1 else sub[0]}"
            grid[name] = select(trades, v, sub)
    return grid


def daily_matrix(grid: Dict[str, List[Dict]]) -> Optional[np.ndarray]:
    """전략별 일간 순수익 (청산일 기준 합). PBO 입력."""
    days = sorted({int((t.get("exit_ts") or 0) // 86400) for g in grid.values() for t in g})
    if len(days) < 40:
        return None
    pos = {d: k for k, d in enumerate(range(days[0], days[-1] + 1))}
    mat = np.zeros((len(grid), len(pos)))
    for row, g in enumerate(grid.values()):
        for t in g:
            mat[row, pos[int((t.get("exit_ts") or 0) // 86400)]] += float(t["net_ret"])
    return mat


def evaluate(trades: Sequence[Dict], patterns: Sequence[str], variant: str,
             paper: Sequence[Dict] = (), live_patterns: Optional[Sequence[str]] = None
             ) -> Dict[str, Any]:
    """
    patterns      : 비교 대상 전체 (종이 매매 패턴). DSR/PBO 의 시행 집합을 만든다.
    live_patterns : 실제로 주문할 패턴. 통과 판정은 이 조합으로 한다.
    """
    from cross_validation import BacktestMetrics as BM

    grid = strategy_grid(trades, patterns)
    by_strategy = {k: summarize([t["net_ret"] for t in g], [t.get("r_net") for t in g])
                   for k, g in grid.items()}

    live_patterns = tuple(live_patterns or patterns)
    chosen = select(trades, variant, live_patterns)
    chosen_rets = [t["net_ret"] for t in chosen]

    srs = [s["sharpe_per_trade"] for s in by_strategy.values()
           if s.get("n", 0) >= 10 and s.get("sharpe_per_trade") is not None]
    dsr = None
    if len(chosen_rets) >= 10 and len(srs) >= 2 and np.var(srs, ddof=1) > 0:
        dsr = BM.calculate_dsr(np.asarray(chosen_rets), num_trials=len(grid),
                               variance_of_trials=float(np.var(srs, ddof=1)))
    mat = daily_matrix(grid)
    pbo = None
    if mat is not None:
        pbo = BM.calculate_pbo(mat, n_splits=16, mc_sims=300,
                               rng=np.random.default_rng(7))

    def split(pred):
        g = [t for t in chosen if pred(t)]
        return summarize([t["net_ret"] for t in g], [t.get("r_net") for t in g])

    by_pattern = {p: split(lambda t, p=p: t["pattern"] == p) for p in live_patterns}
    reasons = {}
    for t in chosen:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    considered = [t for t in trades if t["variant"] == variant and t["pattern"] in live_patterns]
    if len(set(live_patterns)) > 1:
        considered = non_overlap(considered)
    considered = [t for t in considered if t.get("status") in ("closed", "cancelled")]
    fill_rate = len(chosen) / len(considered) if considered else None

    paper_f = select(paper, variant, live_patterns)
    paper_sum = summarize([t["net_ret"] for t in paper_f], [t.get("r_net") for t in paper_f])

    chosen_sum = summarize(chosen_rets, [t.get("r_net") for t in chosen])
    checks = {
        f"거래 {MIN_TRADES}건 이상": chosen_sum.get("n", 0) >= MIN_TRADES,
        "평균 순수익 > 0": (chosen_sum.get("mean_net") or 0) > 0,
        f"DSR >= {MIN_DSR}": dsr is not None and dsr >= MIN_DSR,
        f"PBO <= {MAX_PBO}": pbo is not None and pbo <= MAX_PBO,
    }
    checks[f"표본 외 종이매매 {MIN_PAPER}건 이상"] = paper_sum.get("n", 0) >= MIN_PAPER
    checks["표본 외 종이매매 평균 > 0"] = (paper_sum.get("mean_net") or 0) > 0

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "variant": variant,
        "patterns": list(live_patterns),
        "paper_patterns": list(patterns),
        "chosen": chosen_sum,
        "fill_rate": round(fill_rate, 3) if fill_rate is not None else None,
        "dsr": round(dsr, 4) if dsr is not None else None,
        "pbo": round(pbo, 4) if pbo is not None else None,
        "num_trials": len(grid),
        "by_pattern": by_pattern,
        "core_vs_alt": {"core": split(lambda t: not t.get("is_alt")),
                        "alt": split(lambda t: bool(t.get("is_alt")))},
        "exit_reasons": reasons,
        "by_strategy": by_strategy,
        "paper": paper_sum,
    }


def load_paper(state_dir: str) -> List[Dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(state_dir, "naked", "trades", "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("source") == "paper":
                    out.append(r)
    return out


def load_backtest(state_dir: str) -> List[Dict]:
    p = backtest_path(state_dir)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_report(state_dir: str, report: Dict[str, Any], meta: Dict[str, Any]) -> str:
    report = {**report, **meta, "generated_at": time.time(),
              "generated_at_kst": datetime.now().isoformat(timespec="seconds")}
    path = os.path.join(state_dir, "naked_validation.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=float)
    os.replace(tmp, path)
    return path


def check_gate(state_dir: str, max_age_days: float, variant: Optional[str] = None,
               live_patterns: Optional[Sequence[str]] = None) -> tuple:
    """
    (통과 여부, 사유). main.py 실주문 게이트.
    variant/live_patterns 를 주면 리포트가 '지금 라이브 설정' 을 평가한 것인지도
    본다 - 설정을 바꾼 직후에는 다른 전략을 평가한 옛 리포트로 열리면 안 된다.
    """
    path = os.path.join(state_dir, "naked_validation.json")
    if not os.path.exists(path):
        return False, "검증 리포트 없음 (봇이 자동 생성 중이거나 python validate_naked.py backtest)"
    try:
        with open(path, encoding="utf-8") as f:
            rep = json.load(f)
    except Exception as e:
        return False, f"검증 리포트 읽기 실패: {e}"
    age = (time.time() - float(rep.get("generated_at", 0))) / 86400
    if age > max_age_days:
        return False, f"검증 리포트가 오래됨 ({age:.0f}일 > {max_age_days:.0f}일)"
    if variant is not None and rep.get("variant") != variant:
        return False, f"리포트 청산방식({rep.get('variant')})이 설정({variant})과 다름 - 재평가 대기"
    if live_patterns is not None and sorted(rep.get("patterns") or []) != sorted(live_patterns):
        return False, "리포트 패턴이 라이브 설정과 다름 - 재평가 대기"
    if not rep.get("passed"):
        failed = [k for k, v in (rep.get("checks") or {}).items() if not v]
        return False, f"검증 미통과: {', '.join(failed)}"
    return True, f"검증 통과 ({age:.1f}일 전, DSR {rep.get('dsr')}, PBO {rep.get('pbo')})"


def print_report(rep: Dict[str, Any]):
    def line(name, s):
        if not s or not s.get("n"):
            print(f"  {name:<34} 거래 없음")
            return
        pf = s.get("profit_factor")
        print(f"  {name:<34} n={s['n']:>4}  승률 {s['win_rate']*100:5.1f}%  "
              f"평균 {s['mean_net']*100:+6.3f}%  합계 {s['total_net']*100:+7.2f}%  "
              f"PF {pf if pf is not None else '-':>5}  평균R {s.get('mean_r')}")

    print("\n" + "=" * 100)
    print(f"선택 전략: 청산 '{rep['variant']}' · 패턴 {', '.join(rep['patterns'])}")
    line("합계", rep["chosen"])
    print(f"  매수스톱 체결률 {rep['fill_rate']}  ·  DSR {rep['dsr']}  ·  PBO {rep['pbo']}"
          f"  ·  시행 수 {rep['num_trials']}")
    print("\n[패턴별]")
    for p, s in rep["by_pattern"].items():
        line(p, s)
    print("\n[코어 vs 알트]")
    for k, s in rep["core_vs_alt"].items():
        line(k, s)
    print("\n[청산 사유]", rep["exit_reasons"])
    print(f"\n[시행 집합: 청산방식 x 패턴 {rep['num_trials']}개]")
    for k, s in sorted(rep["by_strategy"].items()):
        line(k, s)
    print("\n[표본 외 종이매매]")
    line("paper", rep["paper"])
    print("\n[판정]")
    for k, v in rep["checks"].items():
        print(f"  {'통과' if v else '실패'}  {k}")
    print(f"\n  => {'PASSED' if rep['passed'] else 'NOT PASSED'}")
    print("=" * 100)


# ---------------------------------------------------------------------------
async def _universe_now(cfg, top_n: int) -> List[str]:
    from upbit import AsyncUpbit
    from candle_feed import AltUniverse
    client = AsyncUpbit()
    try:
        u = AltUniverse(client, exclude=cfg.TARGET_TICKERS, top_n=top_n,
                        min_trade_krw=cfg.NAKED_ALT_MIN_TRADE_KRW,
                        max_spread=cfg.NAKED_ALT_MAX_SPREAD)
        return await u.refresh()
    finally:
        await client.close()


def main(argv=None):
    os.environ.setdefault("UPBIT_OPEN_API_ACCESS_KEY", "validate")
    os.environ.setdefault("UPBIT_OPEN_API_SECRET_KEY", "validate")
    from config import Config

    ap = argparse.ArgumentParser(description="가격행동 전략 검증")
    ap.add_argument("cmd", choices=["backtest", "report"])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--alts", type=int, default=Config.NAKED_ALT_TOP_N,
                    help="알트 유니버스 크기 (0이면 코어만)")
    ap.add_argument("--tickers", default="", help="쉼표 구분. 주면 유니버스 대신 사용")
    ap.add_argument("--state-dir", default=Config.STATE_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    state_dir = args.state_dir
    os.makedirs(os.path.join(state_dir, "naked"), exist_ok=True)

    if args.cmd == "backtest":
        core = list(Config.TARGET_TICKERS)
        if args.tickers:
            tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
            alts = [t for t in tickers if t not in core]
        else:
            alts = asyncio.run(_universe_now(Config, args.alts)) if args.alts > 0 else []
            tickers = core + alts
        print(f"백테스트 {args.days}일 · 종목 {len(tickers)} (코어 {len(core)} / 알트 {len(alts)})")
        # 봇이 같은 서버에서 돌고 있을 수 있으므로 느린 속도(초당 3회)로 받는다
        trades = asyncio.run(run_backtest(tickers, set(alts), args.days, Config))
        save_backtest(state_dir, trades, {"days": args.days, "tickers": tickers, "alts": alts,
                                          "params": backtest_params(Config)})

    rep = reevaluate(state_dir, Config)
    if rep is None:
        print("백테스트 거래가 없습니다. 먼저 backtest 를 실행하세요.")
        return 1
    print_report(rep)
    print(f"리포트: {os.path.join(state_dir, 'naked_validation.json')}")

    # 메타 모델도 바로 학습 (표본이 모자라면 건너뜀)
    from meta_trainer import NakedMetaTrainer
    res = NakedMetaTrainer(state_dir, os.path.join(state_dir, "naked_meta_model.pkl"),
                           variant=Config.NAKED_EXIT_MODE).train_if_ready()
    print("가격행동 메타 모델:", {k: res.get(k) for k in ("trained", "reason", "n_samples",
                                                         "positive_rate", "cv_auc")})
    return 0 if rep["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
