"""
백테스트 실행: Binance COIN-M 과거 데이터 -> 시행 집합 전체 -> 지표·DSR·PBO·워크포워드.

시행 집합 (DSR 시행 수·PBO 행렬):
  청산 5종 (ladder, ladder_ratchet, zone, split, three_bar) x 방향 3종 (both, long, short) = 15
사전에 정한 기본 전략 = 설정값 (EXIT_MODE=ladder, DIRECTIONS=long,short -> 'ladder|both').
기본 전략을 이 결과를 보고 고르지 않았더라도, 비교한 조합 수만큼 DSR 에 벌점을 준다.

두 번 돌린다:
  - reference : 시작 equity 1 BTC. 계약 수 반올림 영향이 거의 없어 '규칙 자체' 를 잰다.
                 검증 게이트는 이 결과를 쓴다.
  - account   : 실제 계정 규모(기본 PAPER_START_EQUITY_BTC=0.007 BTC). 1계약(100 USD) 단위라
                 위험 예산으로 1계약도 못 사는 신호는 건너뛴다 - 실제 계정이 겪을 결과.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

from ..config.settings import ConfigError, Settings
from ..strategy.ladder import EXIT_VARIANTS
from .data import Dataset, download_all, load_dataset
from .metrics import (by_direction, period_stability, summarize_run, top_trade_dependence,
                      trial_statistics, walk_forward)
from .simulator import Precomputed, SimConfig, Simulator

logger = logging.getLogger(__name__)

DIRECTION_SETS = {"both": (1, -1), "long": (1,), "short": (-1,)}
LISTING_START_MS = 1597042800000          # BTCUSD_PERP onboardDate (exchangeInfo 값)


def chosen_name(settings: Settings) -> str:
    d = {(1, -1): "both", (1,): "long", (-1,): "short"}[tuple(settings.directions)]
    return f"{settings.exit_mode}|{d}"


def sim_config(settings: Settings, variant: str, dirs, equity: float) -> SimConfig:
    return SimConfig(
        variant=variant, directions=tuple(dirs), fractions=settings.ladder_tp_fractions,
        start_equity_btc=equity, risk_fraction=settings.risk_fraction, leverage=settings.leverage,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        taker_fee=settings.taker_fee_rate, slippage_bps=settings.slippage_bps,
        stop_slippage_bps=settings.stop_slippage_bps, stop_trigger=settings.stop_trigger_type,
        tp_trigger=settings.tp_trigger_type, valid_bars=settings.entry_valid_bars,
        max_hold_bars=settings.max_hold_bars, max_exposure_multiple=settings.max_exposure_multiple,
        liq_guard_min_ratio=settings.liq_guard_min_ratio)


async def ensure_data(settings: Settings, refresh: bool = False,
                      log: Callable[[str], None] = print) -> Dict[str, Any]:
    from ..exchange.market_data import MarketData
    from ..exchange.rest_client import BinanceRestClient, endpoints
    check_supported_settings(settings)
    rest_url, _ = endpoints("live")          # 과거 데이터는 항상 실거래 공개 API (키 불필요)
    rest = BinanceRestClient(rest_url)
    try:
        await rest.sync_time()
        return await download_all(MarketData(rest), settings.symbol, LISTING_START_MS,
                                  settings.state_dir, refresh=refresh, log=log)
    finally:
        await rest.close()


def check_supported_settings(settings: Settings) -> None:
    if (settings.signal_interval, settings.zone_interval) != ("1h", "4h"):
        raise ConfigError("백테스트 데이터는 1h/4h 전용 - 다른 시간봉으로 검증할 수 없음")
    if settings.entry_trigger_type != "CONTRACT_PRICE":
        raise ConfigError("백테스트 진입 트리거는 CONTRACT_PRICE 전용")
    if settings.stop_price_protect:
        raise ConfigError("백테스트는 STOP_PRICE_PROTECT=true 지연 발동을 모델링하지 않음")


def run_backtest(settings: Settings, ds: Optional[Dataset] = None,
                 reference_equity: float = 1.0, account_equity: Optional[float] = None,
                 log: Callable[[str], None] = print, save: bool = True) -> Dict[str, Any]:
    t0 = time.time()
    check_supported_settings(settings)
    ds = ds or load_dataset(settings.state_dir, settings.symbol)
    if ds.symbol != settings.symbol or ds.spec.symbol != settings.symbol or \
            ds.ltf.period != settings.signal_period_sec or ds.htf.period != settings.zone_period_sec:
        raise ConfigError("백테스트 데이터 심볼/시간봉이 설정과 다름")
    ltf, htf = ds.ltf, ds.htf
    log(f"데이터: 1h {len(ltf)}봉 ({_d(ltf.t[0])} ~ {_d(ltf.t[-1])}), 4h {len(htf)}봉, "
        f"마크봉 누락 {ds.mark_missing}, 펀딩 {len(ds.funding)}건")
    log("신호 사전 계산 중 (전 구간 1회)...")
    pre = Precomputed(ltf, htf, settings.zone_bars, settings.min_rr, (1, -1),
                      progress=lambda i, n: log(f"  {i}/{n}"))
    log(f"  완료 {time.time() - t0:.0f}s - 신호 {pre.signal_count()}")
    sim = Simulator(ltf, ds.mark, ds.funding, ds.spec, pre)
    runs = {}
    for variant in EXIT_VARIANTS:
        for dname, dirs in DIRECTION_SETS.items():
            cfg = sim_config(settings, variant, dirs, reference_equity)
            runs[cfg.name] = sim.run(cfg)
    chosen = chosen_name(settings)
    summaries = {k: summarize_run(v) for k, v in runs.items()}
    trial = trial_statistics(runs, chosen)
    ch = runs[chosen]
    t_start, t_end = float(ltf.t[int(np.argmax(pre.valid))]), float(ltf.t[-1]) + ltf.period
    wf = walk_forward(runs, chosen, t_start, t_end, folds=6)
    report: Dict[str, Any] = {
        "generated_at": time.time(),
        "symbol": settings.symbol,
        "data": {"start": t_start, "end": t_end, "bars_1h": len(ltf), "bars_4h": len(htf),
                 "mark_missing": ds.mark_missing, "funding_records": len(ds.funding),
                 "meta": ds.meta.get("series", {})},
        "contract": ds.spec.essentials(),
        "strategy_params": settings.strategy_params(),
        "fingerprint": settings.fingerprint(ds.spec.essentials()),
        "chosen": chosen,
        "reference_equity_btc": reference_equity,
        "signals": pre.signal_count(),
        "chosen_summary": summaries[chosen],
        "by_direction": by_direction(ch.trades),
        "stability_year": period_stability(ch.trades, "year"),
        "stability_half": period_stability(ch.trades, "half"),
        "top_dependence": top_trade_dependence(ch.trades, 5),
        "trial": trial,
        "walk_forward": wf,
        "strategies": summaries,
        "assumptions": {
            "intrabar": "진입+손절 같은 봉 -> 손절, 손절+목표 같은 봉 -> 손절, 진입 봉 목표 불인정, 갭은 시가",
            "bar_close_exit_fill": "다음 봉 시가 + 슬리피지",
            "stop_trigger": settings.stop_trigger_type, "tp_trigger": settings.tp_trigger_type,
            "daily_loss_limit_pct": settings.max_daily_loss_pct,
            "fees": {"taker": settings.taker_fee_rate}, "slippage_bps": settings.slippage_bps,
            "stop_slippage_bps": settings.stop_slippage_bps,
            "funding": "실제 펀딩 이력(마크가 x 비율), 펀딩 시각 보유 시",
            "partial_fills": "시장가 전량 체결 가정 (BTCUSD_PERP 유동성 대비 수량이 작음). "
                             "부분청산(TP)은 정수 계약 누적 내림",
        },
        "elapsed_sec": None,
    }
    if account_equity:
        acc_cfg = sim_config(settings, settings.exit_mode, settings.directions, account_equity)
        acc = sim.run(acc_cfg)
        report["account_run"] = {"equity_btc": account_equity, "summary": summarize_run(acc),
                                 "by_direction": by_direction(acc.trades)}
    report["elapsed_sec"] = time.time() - t0
    if save:
        out = Path(settings.state_dir)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "backtest_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=_json_default)
        with open(out / "backtest_trades.jsonl", "w", encoding="utf-8") as f:
            for t in ch.trades:
                f.write(json.dumps(t, ensure_ascii=False, default=_json_default) + "\n")
    return report


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _d(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def load_report(state_dir: str) -> Optional[Dict[str, Any]]:
    p = Path(state_dir) / "backtest_report.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def format_report(rep: Dict[str, Any]) -> str:
    def pct(x):
        return "-" if x is None else f"{x * 100:+.3f}%"

    def num(x, f="{:.3f}"):
        return "-" if x is None else f.format(x)

    lines = []
    s = rep["chosen_summary"]
    lines.append("=" * 96)
    lines.append(f"Binance COIN-M {rep['symbol']} 백테스트 · 기본 전략 {rep['chosen']} · "
                 f"{_d(rep['data']['start'])} ~ {_d(rep['data']['end'])}")
    lines.append(f"계약: contractSize={rep['contract']['contract_size']} "
                 f"tick={rep['contract']['tick_size']} step={rep['contract']['step_size']} "
                 f"minQty={rep['contract']['min_qty']} margin={rep['contract']['margin_asset']}")
    lines.append(f"신호 {rep['signals']} · 거래 {s['n']} · 승률 {num(s.get('win_rate'), '{:.1%}')} · "
                 f"평균 {pct(s.get('avg_return'))} (비용 전 {pct(s.get('avg_gross_return'))}) · "
                 f"중앙값 {pct(s.get('median_return'))} · PF {num(s.get('profit_factor'))}")
    lines.append(f"기대값 {num(s.get('expectancy_btc'), '{:+.8f}')} BTC/거래 · "
                 f"평균 R {num(s.get('expectancy_r'))} · 총수익 {pct(s.get('total_return'))} "
                 f"(시작 {s.get('start_equity_btc')} BTC)")
    lines.append(f"최대낙폭 {pct(s.get('max_drawdown'))} · Sharpe(일간,연율) {num(s.get('sharpe_daily_ann'))} · "
                 f"Sortino {num(s.get('sortino_daily_ann'))} · 거래당 SR {num(s.get('sharpe_per_trade'))}")
    lines.append(f"수수료 {num(s.get('fees_btc'), '{:.6f}')} BTC · 펀딩 {num(s.get('funding_btc'), '{:+.6f}')} BTC · "
                 f"청산 사유 {s.get('exit_reasons')}")
    tr = rep["trial"]
    lines.append(f"DSR {num(tr.get('dsr'), '{:.4f}')} · PBO {num(tr.get('pbo'), '{:.3f}')} · "
                 f"시행 수 {tr.get('num_trials')}")
    bd = rep["by_direction"]
    for k in ("long", "short"):
        x = bd[k]
        lines.append(f"  {k:<5} n={x.get('n', 0):>4} 승률 {num(x.get('win_rate'), '{:.1%}')} "
                     f"평균 {pct(x.get('avg_return'))} PF {num(x.get('profit_factor'))}")
    lines.append("기간 안정성(연도별):")
    for y, v in rep["stability_year"]["periods"].items():
        lines.append(f"  {y}: n={v['n']:>3} 평균 {pct(v['avg_return'])} 승률 {v['win_rate']:.0%} "
                     f"합계 {v['total_net_btc']:+.5f} BTC")
    wf = rep["walk_forward"]
    lines.append(f"워크포워드(6구간 앵커드): 기본전략 OOS 평균 {pct(wf.get('default_oos_avg_return'))} "
                 f"({wf.get('default_oos_trades')}건), 양수 구간 {wf.get('default_positive_folds')}/"
                 f"{wf.get('evaluated_folds')}, 선택=기본 {wf.get('selection_matches_default')}회")
    for f in wf["folds"]:
        lines.append(f"  fold{f['fold']} {_d(f['oos_start'])}~{_d(f['oos_end'])} 선택 {f['selected']} "
                     f"OOS 평균 {pct(f['selected_oos'].get('avg_return'))}({f['selected_oos'].get('n', 0)}) · "
                     f"기본 {pct(f['default_oos'].get('avg_return'))}({f['default_oos'].get('n', 0)})")
    td = rep["top_dependence"]
    lines.append(f"상위 5건 제외 평균: {pct(td.get('avg_without_top'))}")
    lines.append("시행 집합 (청산|방향):")
    for k, v in sorted(rep["strategies"].items(), key=lambda kv: -(kv[1].get("avg_return") or -9)):
        lines.append(f"  {k:<22} n={v.get('n', 0):>4} 평균 {pct(v.get('avg_return'))} "
                     f"PF {num(v.get('profit_factor'))} MDD {pct(v.get('max_drawdown'))} "
                     f"총 {pct(v.get('total_return'))}")
    if "account_run" in rep:
        a = rep["account_run"]["summary"]
        lines.append(f"실계정 규모 {rep['account_run']['equity_btc']} BTC: 거래 {a.get('n', 0)} · "
                     f"건너뜀 {a.get('skipped')} {a.get('skip_reasons')} · 평균 {pct(a.get('avg_return'))} · "
                     f"총 {pct(a.get('total_return'))} · MDD {pct(a.get('max_drawdown'))}")
    lines.append("=" * 96)
    return "\n".join(lines)
