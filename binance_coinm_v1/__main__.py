"""
Binance COIN-M V1 명령줄.

  python -m binance_coinm_v1 check                 공개 API 연결·서버 시간·계약 사양 확인 (키 불필요)
  python -m binance_coinm_v1 check --private       + 계정·잔고·포지션·미체결 조회 (읽기 전용, 키 필요)
  python -m binance_coinm_v1 run                   봇 실행 (기본 EXECUTION_MODE=paper)
  python -m binance_coinm_v1 run --duration 120    120초만 실행 (점검용)
  python -m binance_coinm_v1 backtest              바이낸스 과거 데이터 백테스트 (데이터 자동 증분)
  python -m binance_coinm_v1 validate              백테스트 + 표본 외 종이매매 -> 검증 리포트·게이트
  python -m binance_coinm_v1 status                DB 상태 (거래·신호·스냅샷)
  python -m binance_coinm_v1 gate                  실거래 게이트 상태

저장소 루트(d:\\고도화코인매매봇)에서 실행한다. 설정은 binance_coinm_v1/.env 와 환경변수.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time


def _out(s: str = "") -> None:
    print(s, flush=True)


def _settings():
    from .config import Settings
    return Settings.load()


async def cmd_check(private: bool) -> int:
    from .exchange.contract import resolve_contract
    from .exchange.market_data import MarketData
    from .exchange.rest_client import BinanceRestClient, endpoints
    from .storage.redact import GLOBAL_REDACTOR
    s = _settings()
    GLOBAL_REDACTOR.add(*s.secrets())
    url, ws = endpoints(s.binance_env)
    rest = BinanceRestClient(url, recv_window=s.recv_window_ms)
    try:
        server = await rest.sync_time()
        _out(f"서버 시간 {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(server / 1000))} UTC "
             f"(오프셋 {rest.time_offset_ms}ms) · {url}")
        md = MarketData(rest)
        spec = resolve_contract(await md.exchange_info(), s.symbol)
        _out(f"계약 {spec.symbol} pair={spec.pair} type={spec.contract_type} status={spec.contract_status}")
        _out(f"  contractSize={spec.contract_size} {spec.quote_asset} · marginAsset={spec.margin_asset} "
             f"· base={spec.base_asset} quote={spec.quote_asset}")
        _out(f"  tickSize={spec.tick_size} stepSize={spec.step_size} minQty={spec.min_qty} "
             f"maxQty={spec.max_qty} marketMaxQty={spec.market_max_qty}")
        _out(f"  orderTypes={list(spec.order_types)} maintMargin={spec.maint_margin_pct}%")
        pi = await md.premium_index(s.symbol)
        last, _ = await md.ticker_price(s.symbol)
        _out(f"  last={last} mark={pi.mark_price} index={pi.index_price} funding={pi.last_funding_rate} "
             f"next={time.strftime('%H:%M', time.gmtime(pi.next_funding_time_ms / 1000))} UTC")
        bars = await md.closed_bars(s.symbol, s.signal_interval, 5)
        _out(f"  마지막 마감 {s.signal_interval} 봉 시작 "
             f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(bars.t[-1]))} UTC (형성 중 봉 제외)")
        if private:
            if not s.has_api_keys:
                _out("API 키 없음 - 계정 조회 생략")
                return 0
            from .exchange.binance_gateway import BinanceGateway
            from .execution.live_gate import LiveOrderGate
            gate = LiveOrderGate(s, url)
            prest = BinanceRestClient(url, s.api_key, s.api_secret, recv_window=s.recv_window_ms,
                                      mutation_guard=gate.check)
            prest.time_offset_ms = rest.time_offset_ms
            gw = BinanceGateway(prest, "live" if s.binance_env == "live" else "testnet")
            try:
                acct = await gw.get_account()
                btc = acct.asset(spec.margin_asset)
                _out(f"계정: 지갑 {btc.wallet_balance:.8f} · equity {btc.margin_balance:.8f} · "
                     f"가용 {btc.available_balance:.8f} BTC · canTrade={acct.can_trade}")
                _out(f"  헤지 모드={await gw.get_position_mode()}")
                pos = await gw.get_position(s.symbol)
                _out(f"  포지션 {pos.position_amt if pos else 0} @ {pos.entry_price if pos else '-'} "
                     f"({pos.margin_type if pos else '-'}, {pos.leverage if pos else '-'}x)")
                _out(f"  미체결 일반 {len(await gw.get_open_orders(s.symbol))} · "
                     f"조건부 {len(await gw.get_open_algo_orders(s.symbol))}")
                mk, tk = await gw.get_commission_rate(s.symbol)
                _out(f"  수수료 maker {mk} taker {tk}")
            finally:
                await prest.close()
        return 0
    finally:
        await rest.close()


def cmd_run(duration) -> int:
    from .runtime.bot import Bot
    from .runtime.logging_setup import setup_logging
    s = _settings()
    path = setup_logging(s.state_dir)
    _out(f"로그: {path}")
    _out(f"실행 모드 {s.execution_mode} / {s.binance_env} / {s.symbol}")
    bot = Bot(s)
    try:
        asyncio.run(bot.run(duration=duration))
    except KeyboardInterrupt:
        _out("중단")
    return 0


def cmd_backtest(refresh: bool, equity: float, account_equity) -> int:
    from .backtest.runner import ensure_data, format_report, run_backtest
    s = _settings()
    asyncio.run(ensure_data(s, refresh=refresh, log=_out))
    rep = run_backtest(s, reference_equity=equity,
                       account_equity=account_equity if account_equity is not None
                       else s.paper_start_equity_btc, log=_out)
    _out(format_report(rep))
    _out(f"리포트: {s.state_dir}/backtest_report.json · 거래: {s.state_dir}/backtest_trades.jsonl")
    return 0


def cmd_validate(skip_backtest: bool) -> int:
    from .backtest.runner import ensure_data, format_report, load_report, run_backtest
    from .storage.db import Database
    from .validation.gate import ValidationGate, build_report, paper_trade_returns, save_report
    s = _settings()
    if skip_backtest:
        rep = load_report(s.state_dir)
        if rep is None:
            _out("백테스트 리포트 없음 - backtest 먼저 실행")
            return 1
    else:
        asyncio.run(ensure_data(s, log=_out))
        rep = run_backtest(s, account_equity=s.paper_start_equity_btc, log=_out)
        _out(format_report(rep))
    db = Database(s.db_path)
    try:
        paper = paper_trade_returns(db, float(rep["data"]["end"]), s.symbol)
        vr = build_report(s, rep, paper, rep["contract"])
        path = save_report(s, db, vr)
        _out("\n[바이낸스 COIN-M 검증]")
        for k, v in vr["checks"].items():
            _out(f"  {'통과' if v else '실패'}  {k}")
        _out(f"  => {'PASSED' if vr['passed'] else 'NOT PASSED'} (리포트 {path})")
        ok, why = ValidationGate(s, db, rep["contract"]).check()
        _out(f"검증 게이트: {'열림' if ok else '닫힘'} - {why}")
    finally:
        db.close()
    return 0 if vr["passed"] else 2


def cmd_status() -> int:
    from .storage.db import Database
    from .notifications.telegram import kst
    s = _settings()
    db = Database(s.db_path)
    try:
        mode = s.execution_mode
        act = db.active_positions(mode, s.symbol)
        _out(f"모드 {mode} · 활성 거래 {len(act)}")
        for t in act:
            _out(f"  [{t['trade_id']}] {t['state']} {'LONG' if t['direction'] > 0 else 'SHORT'} "
                 f"qty {t['qty_open']} @ {t.get('entry_avg_price')} 손절 {t.get('stop_price')} "
                 f"목표 {t.get('targets')}")
        closed = db.closed_positions(mode)
        net = sum((t.get("accounting") or {}).get("net_pnl_btc", 0.0) for t in closed)
        _out(f"종료 거래 {len(closed)} · 순손익 합계 {net:+.8f} BTC")
        for t in closed[-5:]:
            a = t.get("accounting") or {}
            _out(f"  [{t['trade_id']}] {t.get('close_reason')} 순 {a.get('net_pnl_btc', 0):+.8f} BTC "
                 f"({a.get('net_pnl_usd', 0):+.2f} USD) 종료 {kst(t['closed_at']) if t.get('closed_at') else '-'}")
        snap = db.latest_account_snapshot(mode)
        if snap:
            _out(f"최근 스냅샷 {kst(snap['ts'])}: equity {snap['equity_btc']:.8f} BTC "
                 f"(= {snap['equity_usd'] or 0:,.2f} USD / {snap['equity_krw'] or 0:,.0f} KRW)")
        for g in db.list_signals(mode, 5):
            _out(f"  신호 {kst(g['close_time'])} {'LONG' if g['direction'] > 0 else 'SHORT'} "
                 f"{g['action']} - {g['reason']}")
        vr = db.latest_validation_report()
        if vr:
            _out(f"검증 리포트 {kst(vr['generated_at'])}: {'PASSED' if vr['passed'] else 'NOT PASSED'}")
    finally:
        db.close()
    return 0


def cmd_gate() -> int:
    from .exchange.rest_client import endpoints
    from .execution.live_gate import LiveOrderGate
    from .storage.db import Database
    from .validation.gate import ValidationGate
    s = _settings()
    db = Database(s.db_path)
    try:
        vr = db.latest_validation_report()
        ess = None
        try:
            import json as _j
            from pathlib import Path
            p = Path(s.state_dir) / "backtest_report.json"
            ess = _j.load(open(p, encoding="utf-8"))["contract"] if p.exists() else None
        except Exception:
            ess = None
        vgate = ValidationGate(s, db, ess or {})
        gate = LiveOrderGate(s, endpoints(s.binance_env)[0], vgate.check)
        st = gate.status()
        _out(f"실거래 게이트: {'열림' if st['open'] else '닫힘'} (모드 {st['execution_mode']}, "
             f"환경 {st['binance_env']})")
        for r in st["reasons"]:
            _out(f"  - {r}")
        ok, why = vgate.check()
        _out(f"바이낸스 검증 게이트: {'통과' if ok else '닫힘'} - {why}")
    finally:
        db.close()
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m binance_coinm_v1",
                                 description="Binance COIN-M Futures 자동매매봇 V1 (기본 종이 매매)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--private", action="store_true", help="계정 읽기 전용 조회 (키 필요)")
    r = sub.add_parser("run")
    r.add_argument("--duration", type=float, default=None)
    b = sub.add_parser("backtest")
    b.add_argument("--refresh-data", action="store_true")
    b.add_argument("--equity", type=float, default=1.0, help="기준 시작 equity (BTC)")
    b.add_argument("--account-equity", type=float, default=None)
    v = sub.add_parser("validate")
    v.add_argument("--skip-backtest", action="store_true")
    sub.add_parser("status")
    sub.add_parser("gate")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return asyncio.run(cmd_check(a.private))
    if a.cmd == "run":
        return cmd_run(a.duration)
    if a.cmd == "backtest":
        return cmd_backtest(a.refresh_data, a.equity, a.account_equity)
    if a.cmd == "validate":
        return cmd_validate(a.skip_backtest)
    if a.cmd == "status":
        return cmd_status()
    if a.cmd == "gate":
        return cmd_gate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
