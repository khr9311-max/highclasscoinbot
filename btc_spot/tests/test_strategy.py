from decimal import Decimal
import pytest
from btc_spot.strategy import signal, DAY_MS
from btc_spot.config import credentials, capital


def candles(direction=1):
    return [[i*DAY_MS, str(1000+direction*i), str(1001+direction*i),
             str(999+direction*i), str(1000+direction*i), "0", (i+1)*DAY_MS-1]
            for i in range(202)]


@pytest.mark.parametrize("direction,target", [(1,"1"),(-1,"0"),(0,"0.5")])
def test_causal_and_ties(direction, target):
    rows = candles(direction)
    # The unfinished current day must not change the target.
    rows[-1][4] = "999999999"
    assert signal(rows, 201*DAY_MS+1000)["target_btc_fraction"] == target


def test_gap_and_stale_fail():
    rows = candles()
    with pytest.raises(ValueError):
        signal(rows[:100] + rows[101:], 202*DAY_MS)
    with pytest.raises(ValueError):
        signal(rows, 204*DAY_MS)


def test_mixed_votes():
    rows = candles()
    rows[-1] = [201*DAY_MS,"1100","1101","1099","1100","0",202*DAY_MS-1]
    assert signal(rows, 202*DAY_MS)["votes"] == [-1,-1,1]
    assert Decimal(signal(rows, 202*DAY_MS)["target_btc_fraction"]) == Decimal(1)/3


def test_credentials_are_explicit_not_env_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "UNRELATED")
    path = tmp_path / "binance.env"
    path.write_text("BINANCE_API_KEY=localkey\nBINANCE_API_SECRET=localsecret\n", encoding="utf-8")
    result = credentials(path)
    assert result.api_key == "localkey"
    assert "localkey" not in repr(result) and "localsecret" not in repr(result)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", True])
def test_invalid_capital(value):
    with pytest.raises(ValueError):
        capital(value)
