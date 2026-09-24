"""Independent persistent-exposure research; reads public CSVs, never .env.

Run: python -m btc_lab.research
Daily closed-candle decisions, next-hour execution, BTC equity denomination.
No imports from the previous bot, take-profit ladders, or its position sizing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .engine import Bar, Config, Funding, Spec, run as simulate

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("momentum_20_60_120", "donchian_20_10", "ema_20_100")
RISK_LEVELS = (("vol25_cap075", .25, .75), ("vol40_cap125", .40, 1.25))
PERIODS = {"development": ("2021-04-01", "2024-01-01"),
           "validation_2024": ("2024-01-01", "2025-01-01"),
           "recent": ("2025-01-01", None), "full": ("2021-04-01", None)}


def ts(date):
    return datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()


def clean(x):
    if isinstance(x, dict):
        return {str(k): clean(v) for k,v in x.items()}
    if isinstance(x, (tuple, list, np.ndarray)):
        return [clean(v) for v in x]
    if isinstance(x, (float, np.floating)):
        return float(x) if math.isfinite(x) else None
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def save(path, obj):
    path.write_text(json.dumps(clean(obj), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_market(cache):
    def candles(path):
        with path.open(encoding="utf-8") as f:
            return {int(r["open_time_ms"]) // 1000: tuple(float(r[k]) for k in ("open","high","low","close"))
                    for r in csv.DictReader(f)}
    contract = candles(cache / "BTCUSD_PERP_1h_contract.csv")
    marks = candles(cache / "BTCUSD_PERP_1h_mark.csv")
    times = sorted(contract)
    if not times or any(b-a != 3600 for a,b in zip(times,times[1:])):
        raise ValueError("Contract history is empty or has missing hours")
    if times[-1]+3600 > datetime.now(timezone.utc).timestamp():
        raise ValueError("The final hourly candle has not closed yet")
    bars, missing = [], 0
    for t in times:
        o,h,l,c = contract[t]
        if t not in marks:
            missing += 1
        mo,mh,ml,mc = marks.get(t,contract[t])
        values = (o,h,l,c,mo,mh,ml,mc)
        if (not all(math.isfinite(v) and v > 0 for v in values) or
                h < max(o,c) or l > min(o,c) or mh < max(mo,mc) or ml > min(mo,mc)):
            raise ValueError("Invalid OHLC")
        bars.append(Bar(t=t,o=o,h=h,l=l,c=c,mark_o=mo,mark_h=mh,mark_l=ml,mark_c=mc))
    funding = []
    with (cache / "BTCUSD_PERP_funding.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            funding.append(Funding(t=int(r["funding_time_ms"])/1000,
                                   rate=float(r["funding_rate"]),
                                   mark=float(r["mark_price"]) if r["mark_price"] else None))
    raw = json.loads((cache / "exchange_info_BTCUSD_PERP.json").read_text(encoding="utf-8"))["symbols"][0]
    filters = {r["filterType"]: r for r in raw["filters"]}
    lot = filters.get("MARKET_LOT_SIZE", filters["LOT_SIZE"])
    spec = Spec(contract_size=float(raw["contractSize"]),qty_step=float(lot["stepSize"]),
                min_qty=float(lot["minQty"]),tick=float(filters["PRICE_FILTER"]["tickSize"]),
                maint_margin_rate=.01)
    return bars, funding, spec, {"mark_missing": missing,
                                "funding_missing_mark": sum(x.mark is None for x in funding)}


@dataclass(frozen=True)
class Day:
    t: int
    o: float
    h: float
    l: float
    c: float


def completed_days(bars):
    groups = {}
    for bar in bars:
        groups.setdefault(int(bar.t)//86400*86400, []).append(bar)
    out = []
    for t, group in sorted(groups.items()):
        if len(group) == 24 and [b.t for b in group] == [t+i*3600 for i in range(24)]:
            out.append(Day(t,group[0].o,max(b.h for b in group),min(b.l for b in group),group[-1].c))
    return out


def ema(values, length):
    result = np.empty(len(values))
    if len(values):
        result[0] = values[0]
        alpha = 2/(length+1)
        for i in range(1,len(values)):
            result[i] = alpha*values[i]+(1-alpha)*result[i-1]
    return result


def target_schedule(days, family, vol_target, exposure_cap):
    """Data through day j close can only place a target at day j+1 open."""
    if family not in (*FAMILIES,"long_vol_benchmark"):
        raise ValueError("Unregistered research family")
    closes = np.asarray([x.c for x in days])
    logs = np.diff(np.log(closes))
    fast,slow = ema(closes,20),ema(closes,100)
    targets = {}
    state = 0
    for j in range(200,len(days)):
        if any(days[k].t-days[k-1].t != 86400 for k in range(j-120+1,j+1)):
            continue
        if family == "momentum_20_60_120":
            strength = sum(float(np.sign(closes[j]/closes[j-lag]-1)) for lag in (20,60,120))/3
        elif family == "ema_20_100":
            strength = float(np.sign(fast[j]-slow[j]))
        elif family == "long_vol_benchmark":
            strength = 1.0
        else:
            if closes[j] > max(x.h for x in days[j-20:j]):
                state = 1
            elif closes[j] < min(x.l for x in days[j-20:j]):
                state = -1
            elif state == 1 and closes[j] < min(x.l for x in days[j-10:j]):
                state = 0
            elif state == -1 and closes[j] > max(x.h for x in days[j-10:j]):
                state = 0
            strength = state
        vol = float(logs[j-30:j].std(ddof=1)*math.sqrt(365))
        amplitude = min(exposure_cap,vol_target/max(vol,.10))
        targets[days[j].t+86400] = float(strength*amplitude)
    return targets


def period_input(bars, funding, targets, start, end):
    selected = [b for b in bars if start <= b.t < end]
    return selected, [f for f in funding if start <= f.t < end], {
        t:v for t,v in targets.items() if start <= t < end}


def enrich(result):
    s = dict(result["summary"])
    curve = result["equity_curve"]
    e = np.asarray([r["equity_btc"] for r in curve])
    timestamps = np.asarray([r["t"] for r in curve])
    daily_last = {}
    for t,x in zip(timestamps[1:],e[1:]):
        daily_last[(int(t)-1)//86400] = float(x)
    daily = np.array([s["initial_btc"], *daily_last.values()])
    returns = np.diff(daily)/daily[:-1]
    years = max((timestamps[-1]-timestamps[0])/(365.25*86400),1/365.25)
    ratio = s["final_btc"]/s["initial_btc"]
    s["cagr_pct"] = (ratio**(1/years)-1)*100 if ratio > 0 else -100
    s["daily_sharpe"] = float(returns.mean()/returns.std(ddof=1)*math.sqrt(365)) if len(returns)>2 and returns.std(ddof=1)>0 else None
    s["days"] = len(daily_last)
    s["fraction_hours_in_position"] = float(np.mean([r["position"] != 0 for r in curve]))
    s["mean_absolute_exposure"] = float(np.mean([abs(r["exposure"]) for r in curve]))
    s["max_absolute_exposure"] = float(max(abs(r["exposure"]) for r in curve))
    s["btc_gain"] = s["final_btc"]-s["initial_btc"]
    quarters = {}
    previous = s["initial_btc"]
    for t,x in zip(timestamps[1:],e[1:]):
        d = datetime.fromtimestamp(float(t)-1,timezone.utc)
        quarters[f"{d.year}Q{(d.month-1)//3+1}"] = float(x)
    qr = {}
    for q,x in quarters.items():
        qr[q] = (x/previous-1)*100 if previous>0 else None
        previous=x
    s["quarter_returns_pct"] = qr
    return s


def pick_development(rows):
    candidates = []
    for name, data in rows.items():
        if name == "long_vol_benchmark":
            continue
        train = data["development"]
        if all(train[c]["return_pct"]>0 and train[c]["max_drawdown_pct"]<=35
               and not train[c]["bankrupt"] and train[c]["liquidations"]==0
               for c in ("base","stress_2x")):
            candidates.append((train["base"]["cagr_pct"],name))
    winner = max(candidates)[1] if candidates else None
    passed = winner is not None and all(rows[winner]["validation_2024"][c]["return_pct"]>0
                and rows[winner]["validation_2024"][c]["max_drawdown_pct"]<=35
                and not rows[winner]["validation_2024"][c]["bankrupt"]
                and rows[winner]["validation_2024"][c]["liquidations"]==0
                for c in ("base","stress_2x"))
    return {"development_choice":winner,"passed_validation_screen":passed,
            "recent_used_for_selection":False,"live_eligible":False}


def run(cache,output):
    output.mkdir(parents=True,exist_ok=True)
    bars,funding,spec,data_quality = read_market(cache)
    end = bars[-1].t+3600
    cases = [(f"{family}_{label}",family,vol,cap) for family in FAMILIES
             for label,vol,cap in RISK_LEVELS]
    cases.append(("long_vol_benchmark","long_vol_benchmark",.25,.75))
    protocol = {"created_at":datetime.now(timezone.utc).isoformat(),
        "objective":"BTC net equity after all modeled costs, not USD gains or guaranteed maximum return",
        "independent_engine":True,"history_previously_viewed":True,
        "families":FAMILIES,"cases":cases,"periods":PERIODS,"initial_btc":.007,
        "rules":{"momentum":"Average signs of 20/60/120 day returns",
                 "donchian":"20-day closed breakout, opposite10-day exit, persistent position",
                 "ema":"Persistent sign of EMA20 minus EMA100, not crossing-event-only",
                 "volatility":"30-day daily log-return volatility, annualized sqrt365, floor10%",
                 "allocation":"Signed strength x min(cap, annual target / observed volatility), daily integer resizing",
                 "protection":"Fixed10% price stop, BTC account margin/maintenance simulation; no fixed TP/holding time"},
        "risk_levels":RISK_LEVELS,"leverage":3,"fee":.0005,"slip_bps":3,"stop_slip_bps":10,
        "cost_stress":"Fee, ordinary/stop slippage doubled; new integer sizing and paths",
        "intrabar_funding_policy":"adverse: retain funding debits and omit uncertain credits in protective-exit hours",
        "selection":"Only development base AND stress positive/no liquidation/BTC DD<=35%; highest development BTC CAGR. "
                    "Then validation2024 same screen. Recent results do not choose or replace a candidate.",
        "comparison_count_known_at_least":41,"new_candidate_cases":6,"new_long_control":1,
        "contract_spec":asdict(spec),"data_quality":data_quality,
        "data_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(cache.iterdir())
                       if p.name.startswith(('BTCUSD_PERP','exchange_info_BTCUSD_PERP'))},
        "source_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (Path(__file__),Path(__file__).with_name('engine.py'))},
        "limitations":["Exploratory reused history; this is not untouched OOS.",
          "Higher exposure intentionally differs from the old0.5%-per-entry sizing; greater losses are possible.",
          "Whole BTC wallet supports margin in this model. Not proven equivalent to the production isolated account.",
          "Target volatility is not a promised realized volatility or loss ceiling.",
          "Stops, slippage, maintenance and liquidation costs are approximations, not order-book replay.",
          "BTC drawdown is sampled at hourly closes; intrahour peaks/troughs can imply greater drawdown.",
          "Current contract sizes/filters used historically; absent funding marks use hourly mark prices.",
          "Maintenance rate1% and liquidation fee0.5% are research assumptions, not account-specific verified brackets. exchangeInfo maintMarginPercent is ignored as instructed by Binance.",
          "No live connector, credentials, capital transfer, validation override, or messaging."]}
    protocol_path=output/'protocol.json'
    if protocol_path.exists():
        prior=json.loads(protocol_path.read_text(encoding='utf-8'))
        without_date=lambda p:{k:v for k,v in clean(p).items() if k!='created_at'}
        if without_date(prior)!=without_date(protocol):
            raise ValueError("Protocol changed; use a new output folder for revised research")
        protocol=prior
    else:
        save(protocol_path,protocol)
    days=completed_days(bars)
    rows={}
    for name,family,vol,cap in cases:
        print(name,flush=True)
        targets=target_schedule(days,family,vol,cap)
        rows[name]={}
        for period,(begin,finish) in PERIODS.items():
            start,stop=ts(begin),ts(finish) if finish else end
            b,f,t=period_input(bars,funding,targets,start,stop)
            rows[name][period]={}
            for cost,factor in (("base",1),("stress_2x",2)):
                cfg=Config(initial_btc=.007,fee=.0005*factor,slip_bps=3*factor,
                           stop_slip_bps=10*factor,max_exposure=cap,leverage=3,stop_pct=.10,
                           intrabar_funding_policy='adverse')
                result=simulate(b,f,t,spec,cfg)
                rows[name][period][cost]=enrich(result)
                save(output/f"{name}_{period}_{cost}.json",result)
        print("  recent",rows[name]['recent']['base'],flush=True)
        save(output/'partial_results.json',rows)
    report={"protocol":protocol,"results":rows,"selection":pick_development(rows)}
    save(output/'report.json',report)
    lines=["# 새 BTC 목표노출 연구", "", "모든 수익률·낙폭은 BTC 기준. 0.007 BTC 시작, 실거래 적격 판정 아님.", ""]
    for period in PERIODS:
        lines += [f"## {period}","","| 후보 | BTC 수익 | 비용2배 | BTC 최대낙폭 | 평균 절대노출 | 보유시간 비중 |",
                  "|---|---:|---:|---:|---:|---:|"]
        for name,data in rows.items():
            s=data[period]['base'];stress=data[period]['stress_2x']
            lines.append(f"| {name} | {s['return_pct']:+.2f}% | {stress['return_pct']:+.2f}% | "
                         f"{s['max_drawdown_pct']:.2f}% | {s['mean_absolute_exposure']:.3f}x | {s['fraction_hours_in_position']:.1%} |")
        lines.append("")
    lines += ["## 선택", "", json.dumps(report['selection'],ensure_ascii=False), "",
              "단순 BTC 보유는 BTC 수량 수익0%. long_vol_benchmark는 추세예측 없이 지속 롱인 별도 위험 벤치마크."]
    (output/'comparison.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,default=ROOT/'binance_coinm_v1/state/cache')
    parser.add_argument('--output',type=Path,default=ROOT/'btc_lab/state/rebuild_20260924_v2')
    args=parser.parse_args()
    run(args.cache,args.output)
