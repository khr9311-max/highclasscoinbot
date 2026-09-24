"""Closed-bar signals. Prices of alts are in BTC, never inferred from USD gains."""
from decimal import Decimal
from btc_spot.store import number


def closed(rows, now, period, minimum):
    end = int(now)//period*period
    bars = [r for r in rows if int(r[0]) < end and int(r[6]) < int(now)]
    if len(bars) < minimum or int(bars[-1][0]) != end-period:
        raise ValueError("Missing fresh completed candles")
    bars = bars[-minimum:]
    for i, row in enumerate(bars):
        o, h, low, c = map(number, row[1:5])
        if (int(row[0]) % period or int(row[6]) != int(row[0])+period-1
                or min(o, h, low, c) <= 0 or low > min(o, c) or h < max(o, c)
                or i and int(row[0])-int(bars[i-1][0]) != period):
            raise ValueError("Invalid or discontinuous closed candles")
    return bars


def ema(prices, span):
    result = prices[0]
    alpha = Decimal(2)/Decimal(span+1)
    for price in prices[1:]:
        result += alpha*(price-result)
    return result


def coinm_signal(market):
    bars = closed(market["klines"], market["server_time_ms"], 14_400_000, 200)
    prices = [number(r[4]) for r in bars]
    fast, slow = ema(prices, 20), ema(prices, 80)
    direction = 1 if fast > slow else -1 if fast < slow else 0
    ranges = [max(number(bars[i][2])-number(bars[i][3]),
                  abs(number(bars[i][2])-prices[i-1]), abs(number(bars[i][3])-prices[i-1]))
              for i in range(len(bars)-14, len(bars))]
    distance = max(Decimal(".01"), min(Decimal(".08"), sum(ranges)/14*2/prices[-1]))
    return {"bar": str(bars[-1][0]), "direction": direction, "stop_fraction": str(distance)}


def alt_signal(markets):
    scores, epochs = {}, set()
    for symbol, market in markets.items():
        bars = closed(market["klines"], market["server_time_ms"], 86_400_000, 61)
        epochs.add(int(bars[-1][0]))
        latest = number(bars[-1][4])
        scores[symbol] = (latest/number(bars[-21][4])-1 + latest/number(bars[-61][4])-1)/2
    if len(epochs) != 1:
        raise ValueError("Alt signals must share a completed day")
    winner = max(sorted(scores), key=scores.get)
    return {"bar": str(epochs.pop()), "symbol": winner if scores[winner] > 0 else None,
            "scores": {s: str(v) for s, v in scores.items()}}
