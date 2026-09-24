"""11단계: COIN-M 시뮬레이터 규칙과 지표 (합성 데이터, 네트워크 없음)."""

import math

import numpy as np
import pytest

from binance_coinm_v1.backtest.metrics import (daily_returns, deflated_sharpe, max_drawdown,
                                               pbo_cscv, summarize_trades, walk_forward)
from binance_coinm_v1.backtest.simulator import SimConfig, Simulator
from binance_coinm_v1.exchange.market_data import FundingRecord
from binance_coinm_v1.risk import inverse_math as im
from binance_coinm_v1.strategy.signals import TradeSignal

from .helpers import load_spec
from .synth import bars_from

SPEC = load_spec()
P = 3600
T0 = 1_699_999_200            # 정시 (3600 의 배수) - 실제 바이낸스 봉과 같게


class FakePre:
    def __init__(self, n, signals, exit_long=(), exit_short=()):
        self.n = n
        self.valid = np.ones(n, dtype=bool)
        self.valid[:61] = False
        self.long = [None] * n
        self.short = [None] * n
        for (i, d), s in signals.items():
            (self.long if d > 0 else self.short)[i] = s
        self.exit_long = np.zeros(n, dtype=bool)
        self.exit_short = np.zeros(n, dtype=bool)
        self.exit_long[list(exit_long)] = True
        self.exit_short[list(exit_short)] = True

    def entry(self, i, d):
        return self.long[i] if d > 0 else self.short[i]


def sig(i, d, entry, stop, targets):
    return TradeSignal("trendy_kangaroo", d, i, T0 + i * P, T0 + (i + 1) * P, entry, stop,
                       list(targets), 100.0, {})


def rows_flat(n, px=80000.0):
    return [(px, px + 80, px - 80, px)] * n


def run_sim(rows, signals, cfg=None, mark_rows=None, funding=(), **pre_kw):
    b = bars_from(rows, P, t0=T0)
    m = bars_from(mark_rows, P, t0=T0) if mark_rows else None
    pre = FakePre(len(rows), signals, **pre_kw)
    cfg = cfg or SimConfig(slippage_bps=0, stop_slippage_bps=0)
    return Simulator(b, m, list(funding), SPEC, pre).run(cfg)


def test_same_bar_entry_and_stop_counts_as_loss():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79500, 79900)                 # 트리거와 손절 모두 닿음
    res = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [81000.0])})
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["exit_reason"] == "stop" and t["fill_px"] == 80200.0
    assert t["exits"][0][2] == 79600.0
    qty = t["qty"]
    exp = im.pnl_btc(1, qty, 100, 80200.0, 79600.0)
    assert t["realized_btc"] == pytest.approx(exp)
    fees = im.fee_btc(qty, 100, 80200.0, 0.0005) + im.fee_btc(qty, 100, 79600.0, 0.0005)
    assert t["fee_btc"] == pytest.approx(fees)
    assert res.final_equity == pytest.approx(1.0 + exp - fees)
    assert isinstance(qty, int) and qty >= 1


def test_no_target_on_entry_bar_then_tp_next_bar():
    rows = rows_flat(100)
    rows[71] = (80000, 81500, 79950, 81400)                 # 진입 봉에서 TP1 넘음 -> 불인정
    rows[72] = (81400, 81100, 80900, 81000)
    rows[72] = (81000, 81100, 80900, 81050)                 # 다음 봉에서 TP1 81000 도달
    res = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [81000.0, 82000.0, 83000.0])})
    t = res.trades[0]
    tp = [e for e in t["exits"] if e[0] == "tp1"]
    assert tp and tp[0][2] == 81000.0
    assert sum(q for _, q, _ in t["exits"]) == t["qty"]      # 결국 전량 청산 (정수 계약)


def test_stop_beats_target_in_same_bar_and_gap_fills_at_open():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80250)                 # 진입
    rows[72] = (80250, 81100, 79500, 80000)                 # 손절·목표 모두 -> 손절
    res = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [81000.0])})
    assert res.trades[0]["exit_reason"] == "stop" and res.trades[0]["exits"][0][2] == 79600.0
    rows[72] = (79000, 79100, 78800, 78900)                 # 갭 하락 -> 시가 체결
    res2 = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [81000.0])})
    assert res2.trades[0]["exits"][0][2] == 79000.0


def test_mark_price_stop_trigger_uses_mark_series():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80250)
    rows[72] = (80250, 80300, 79500, 80000)                 # 체결가는 손절 아래
    marks = [r for r in rows]
    marks[72] = (80250, 80300, 79700, 80000)                # 마크는 손절 위 -> 안 터짐
    cfg = SimConfig(slippage_bps=0, stop_slippage_bps=0, stop_trigger="MARK_PRICE")
    res = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [])}, cfg, mark_rows=marks)
    assert res.trades[0]["exits"][0][0] != "stop" or res.trades[0]["exit_ts"] > T0 + 73 * P
    cfg2 = SimConfig(slippage_bps=0, stop_slippage_bps=0, stop_trigger="CONTRACT_PRICE")
    res2 = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [])}, cfg2, mark_rows=marks)
    assert res2.trades[0]["exit_reason"] == "stop"


def test_stop_before_entry_and_expiry_cancel():
    rows = rows_flat(100)
    rows[71] = (80000, 80100, 79500, 79900)                 # 트리거 전 손절선
    assert run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79600.0, [])}).trades == []
    rows2 = rows_flat(100)                                  # 트리거 없음 -> 2봉 뒤 만료
    rows2[74] = (80000, 80300, 79950, 80250)
    assert run_sim(rows2, {(70, 1): sig(70, 1, 80200.0, 79600.0, [])}).trades == []


def test_funding_applied_while_holding():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80250)
    fr = FundingRecord("BTCUSD_PERP", (T0 + 75 * P) * 1000 + 4, 0.0003, 80000.0)
    res = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 79000.0, [])}, funding=[fr])
    t = res.trades[0]
    assert t["funding_btc"] == pytest.approx(-t["qty"] * 100 / 80000.0 * 0.0003)
    assert t["net_btc"] == pytest.approx(t["realized_btc"] - t["fee_btc"] + t["funding_btc"])


def test_short_mirror_structure():
    rows = rows_flat(100)
    rows[71] = (80000, 80050, 79700, 79750)                 # 숏 트리거 79800
    rows[72] = (79750, 79800, 78900, 78950)                 # TP1 79000
    rows[73] = (78950, 80600, 78900, 80500)                 # 손절 80400
    res = run_sim(rows, {(70, -1): sig(70, -1, 79800.0, 80400.0, [79000.0, 78000.0])})
    t = res.trades[0]
    assert t["side"] == "SHORT" and t["fill_px"] == 79800.0
    assert t["exits"][0][0] == "tp1" and t["exits"][0][2] == 79000.0
    assert t["exit_reason"] == "ladder_stop" or t["exit_reason"].endswith("stop")
    assert im.pnl_btc(-1, 1, 100, 79800.0, 79000.0) > 0


def test_opposite_signal_exits_without_instant_reversal():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80250)
    s_long = sig(70, 1, 80200.0, 79000.0, [85000.0])
    s_short1 = sig(75, -1, 79700.0, 80500.0, [])
    s_short2 = sig(78, -1, 79700.0, 80500.0, [])
    rows[79] = (80000, 80050, 79600, 79650)                 # 두 번째 숏 신호의 트리거
    res = run_sim(rows, {(70, 1): s_long, (75, -1): s_short1, (78, -1): s_short2})
    kinds = [(t["side"], t["exit_reason"]) for t in res.trades]
    assert kinds[0] == ("LONG", "opposite_signal")
    assert res.trades[0]["exit_ts"] == T0 + 76 * P
    assert len(res.trades) == 2 and res.trades[1]["side"] == "SHORT"
    assert res.trades[1]["signal_ts"] == s_short2.close_time      # 첫 숏 신호는 청산에만 사용


def test_min_qty_skip_at_small_equity():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80250)
    cfg = SimConfig(slippage_bps=0, stop_slippage_bps=0, start_equity_btc=0.0005)
    res = run_sim(rows, {(70, 1): sig(70, 1, 80200.0, 77000.0, [])}, cfg)
    assert res.trades == [] and "최소 수량" in res.skipped[0]["skip_reason"]


def test_ladder_variants_share_signals():
    rows = rows_flat(100)
    rows[71] = (80000, 80300, 79950, 80250)
    for k in range(72, 99):
        px = 80250 + (k - 71) * 120
        rows[k] = (px - 100, px + 60, px - 130, px + 40)
    s = {(70, 1): sig(70, 1, 80200.0, 79600.0, [81000.0, 82000.0, 83000.0])}
    out = {}
    for v in ("ladder", "ladder_ratchet", "zone", "split", "three_bar"):
        res = run_sim(rows, s, SimConfig(variant=v, slippage_bps=0, stop_slippage_bps=0))
        out[v] = res.trades[0]
    assert out["zone"]["exits"][0][0] == "tp1" and len(out["zone"]["exits"]) == 1
    assert [e[0] for e in out["split"]["exits"]][:2] == ["tp1", "tp2"]
    assert all(e[0] != "tp1" for e in out["ladder_ratchet"]["exits"])
    assert [e[0] for e in out["ladder"]["exits"]][:3] == ["tp1", "tp2", "tp3"]


# ---------------------------------------------------------------- 지표
def test_drawdown_and_daily_returns():
    mdd, mdd_btc = max_drawdown([1.0, 1.2, 0.9, 1.3, 1.1])
    assert mdd == pytest.approx(0.25) and mdd_btc == pytest.approx(0.3)
    ts = np.array([0, 3600, 86400, 90000, 2 * 86400], dtype=float)
    r, days = daily_returns([1.0, 1.1, 1.2, 1.0, 1.5], ts)
    assert list(days) == [1, 2] and r[0] == pytest.approx(1.0 / 1.1 - 1) and r[1] == pytest.approx(0.5)


def test_dsr_and_pbo_behave():
    rng = np.random.default_rng(1)
    good = rng.normal(0.004, 0.01, 300)
    noise = rng.normal(0.0, 0.01, 300)
    assert deflated_sharpe(good, 15, 0.01) > 0.95
    assert deflated_sharpe(noise, 15, 0.01) < 0.5
    # 실력 없는 전략들: 표본 하나의 PBO 는 분산이 크다 -> 여러 표본 평균이 0.5 근처
    avg = np.mean([pbo_cscv(np.random.default_rng(s).standard_normal((15, 800)), mc_sims=100)
                   for s in range(12)])
    assert 0.3 < avg < 0.65
    strong = rng.standard_normal((15, 800)) * 0.01
    strong[3] += 0.01                                                     # 한 전략만 진짜 우위
    assert pbo_cscv(strong) < 0.1


def test_summarize_and_walk_forward_mechanics():
    class R:
        def __init__(self, trades):
            self.trades = trades
    mk = lambda ts, ret: {"signal_ts": ts, "ret": ret, "net_btc": ret, "net_usd": ret * 80000,
                          "r": ret / 0.005, "bars_held": 5, "fee_btc": 0.0, "funding_btc": 0.0,
                          "direction": 1}
    good = [mk(i * 10, 0.01 if i % 3 else -0.004) for i in range(120)]
    bad = [mk(i * 10, -0.01 if i % 3 else 0.004) for i in range(120)]
    runs = {"a|both": R(good), "b|both": R(bad)}
    wf = walk_forward(runs, "a|both", 0, 1200, folds=6)
    assert wf["evaluated_folds"] == 5 and wf["selection_matches_default"] == 5
    assert wf["default_positive_folds"] == 5
    s = summarize_trades(good)
    assert s["n"] == 120 and s["profit_factor"] > 1 and 0 < s["win_rate"] < 1


def test_dsr_pbo_match_reference_implementation():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "cross_validation.py"
    if not path.exists():
        pytest.skip("원본 cross_validation.py 없음")
    from .test_strategy_parity import load_original
    BM = load_original(path, "upbit_cross_validation_original").BacktestMetrics
    rng = np.random.default_rng(5)
    r = rng.normal(0.001, 0.01, 200)
    assert deflated_sharpe(r, 15, 0.004) == pytest.approx(BM.calculate_dsr(r, 15, 0.004))
    mat = rng.standard_normal((15, 640))
    assert pbo_cscv(mat, 16, 120, rng=np.random.default_rng(3)) == \
        BM.calculate_pbo(mat, 16, 120, rng=np.random.default_rng(3))


def test_data_cache_roundtrip_with_fake_exchange(tmp_path):
    import json as _json
    from binance_coinm_v1.backtest.data import download_all, load_dataset
    from binance_coinm_v1.exchange import BinanceRestClient
    from binance_coinm_v1.exchange.market_data import MarketData
    from .conftest import run
    from .fakes import FakeTransport, no_sleep
    from .helpers import FIXTURES

    H = 3_600_000
    start = 1_700_000_000_000 - (1_700_000_000_000 % (4 * H))
    now = start + 400 * H + 1000

    def kl(p, step):
        s, e = int(p["startTime"]), int(p["endTime"])
        out = []
        t = s - (s - start) % step if s > start else start
        while t <= e and t + step <= now + step:
            px = 30000 + (t - start) / H
            out.append([t, str(px), str(px + 5), str(px - 5), str(px + 1), "10", t + step - 1,
                        "0.1", 3, "5", "0.05", "0"])
            t += step
        return out[:int(p["limit"])]

    xi = _json.load(open(FIXTURES / "exchange_info_coinm.json", encoding="utf-8"))
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/time", {"serverTime": now})
    tr.add("GET", "/dapi/v1/exchangeInfo", lambda p: xi)
    tr.add("GET", "/dapi/v1/klines", lambda p: kl(p, 4 * H if p["interval"] == "4h" else H))
    tr.add("GET", "/dapi/v1/markPriceKlines", lambda p: kl(p, H))
    tr.add("GET", "/dapi/v1/fundingRate", lambda p: [
        {"symbol": "BTCUSD_PERP", "fundingTime": start + k * 8 * H, "fundingRate": "0.0001",
         "markPrice": ""} for k in range(50) if start + k * 8 * H >= int(p["startTime"])][:1000])
    rest = BinanceRestClient("https://dapi.binance.com", transport=tr, clock=lambda: now / 1000,
                             sleep=no_sleep)

    async def go():
        await rest.sync_time()
        return await download_all(MarketData(rest), "BTCUSD_PERP", start, str(tmp_path),
                                  log=lambda m: None, pause=0)

    meta = run(go())
    assert meta["series"]["1h_contract"]["bars"] == 400 and meta["series"]["1h_contract"]["gaps"] == 0
    ds = load_dataset(str(tmp_path), "BTCUSD_PERP")
    assert len(ds.ltf) == 400 and len(ds.htf) == 100 and ds.mark_missing == 0
    assert ds.spec.contract_size == 100 and len(ds.funding) == 50
    assert all(ds.ltf.close_time(i) <= now / 1000 for i in range(len(ds.ltf)))   # 마감 봉만
