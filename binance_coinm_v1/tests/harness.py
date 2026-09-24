"""엔진 테스트 하네스: 종이 게이트웨이 + 가짜 시계 + 기록 알림."""

from decimal import Decimal

from binance_coinm_v1.config import Settings
from binance_coinm_v1.exchange.paper_gateway import PaperGateway
from binance_coinm_v1.execution.engine import Engine
from binance_coinm_v1.notifications import RecordingNotifier
from binance_coinm_v1.storage import Database
from binance_coinm_v1.strategy.signals import TradeSignal

from .fakes import no_sleep
from .helpers import load_spec
from .synth import bars_from

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


class Harness:
    def __init__(self, tmp_path, equity=0.05, db=None, paper=None, clock=None, **over):
        kw = dict(state_dir=str(tmp_path / "state"), slippage_bps=0, stop_slippage_bps=0)
        kw.update(over)
        self.settings = Settings.build(**kw)
        self.spec = load_spec()
        self.clock = clock or Clock()
        self.paper = paper or PaperGateway(self.spec, self.settings, clock=self.clock,
                                           start_equity_btc=equity)
        self.db = db or Database(str(tmp_path / "engine.sqlite3"))
        self.notes = RecordingNotifier()
        self.engine = Engine(self.settings, self.paper, self.db, self.spec, self.notes,
                             clock=self.clock, mono=self.clock, sleep=no_sleep)

    async def start(self, price=80000.0):
        await self.tick(price)
        rep = await self.engine.startup()
        return rep

    async def tick(self, last, mark=None, dt=1.0):
        self.clock.t += dt
        mark = last if mark is None else mark
        ts = int(self.clock.t * 1000)
        self.paper.update_market(last=last, mark=mark, index=mark, ts_ms=ts)
        await self.engine.on_market(last=last, mark=mark, index=mark, ts_ms=ts)
        await self.engine.drain_user_events()

    def arm(self, direction, entry, stop, targets, close_time=None):
        close_time = close_time if close_time is not None else self.clock.t - 1
        sig = TradeSignal("trendy_kangaroo", direction, 100, close_time - 3600, close_time,
                          entry, stop, list(targets), 500.0, {})
        t = self.engine.entries.arm(sig, None)
        self.engine.trade = t
        return t

    @property
    def trade(self):
        return self.engine.trade

    def algos(self, status=("NEW",)):
        return [a for a in self.paper.algos.values() if a["status"] in status]

    def position(self):
        return self.paper.pos_qty

    def states(self, trade_id):
        return [r["to_state"] for r in self.db.transitions(trade_id)]


def flat_bars(n, price, t_end, period=3600, highs=None, lows=None, bullish=True):
    """
    t_end 에 마감하는 n 개 봉. highs/lows 로 특정 봉을 지정한다.
    bullish=True 면 지정한 봉을 '종가가 고가 근처인 양봉' 으로 만든다 (윗꼬리만 긴 봉은
    약세 캥거루 꼬리 = 반전 청산 신호가 되므로 테스트 의도와 달라진다).
    """
    rows = [(price, price * 1.001, price * 0.999, price)] * n
    rows = list(rows)
    for k in set((highs or {}).keys()) | set((lows or {}).keys()):
        o, h, l, c = rows[k]
        h = (highs or {}).get(k, h)
        l = (lows or {}).get(k, l)
        if bullish:
            o, c = l + (h - l) * 0.1, h - (h - l) * 0.05
        rows[k] = (o, max(h, o, c), min(l, o, c), c)
    t0 = t_end - n * period
    return bars_from(rows, period, t0=t0)
