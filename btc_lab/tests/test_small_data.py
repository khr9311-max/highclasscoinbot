import pytest

from btc_lab.small_data import HOUR_MS, public_get, validate_chunk


def candle(t=0):
    return [t, '10', '12', '9', '11', '1', t+HOUR_MS-1]


def test_spot_history_does_not_silently_fill_missing_or_duplicate_candles():
    for rows in ([candle()], [candle(), candle()], [candle(HOUR_MS), candle()]):
        with pytest.raises(ValueError, match='Missing, duplicated or reordered'):
            validate_chunk(rows, 0, 2*HOUR_MS)
    assert len(validate_chunk([candle(), candle(HOUR_MS)], 0, 2*HOUR_MS)) == 2


def test_open_or_nonfinite_spot_candle_rejected():
    row = candle()
    row[6] -= 1
    with pytest.raises(ValueError, match='close timestamp'):
        validate_chunk([row], 0, HOUR_MS)
    row = candle()
    row[2] = 'NaN'
    with pytest.raises(ValueError, match='OHLC'):
        validate_chunk([row], 0, HOUR_MS)


def test_public_downloader_cannot_access_orders_or_authentication():
    with pytest.raises(ValueError):
        public_get('/api/v3/order')
    with pytest.raises(ValueError):
        public_get('/api/v3/time', {'signature':'unused'})


def test_explicit_gap_mode_keeps_only_observed_bars_and_still_rejects_duplicates():
    rows = [candle(), candle(2*HOUR_MS)]
    assert validate_chunk(rows, 0, 3*HOUR_MS, allow_gaps=True) == rows
    with pytest.raises(ValueError):
        validate_chunk([candle(), candle()], 0, 3*HOUR_MS, allow_gaps=True)


def test_historical_partial_bar_is_only_accepted_when_explicitly_requested():
    row = candle()
    row[6] = HOUR_MS//2
    with pytest.raises(ValueError):
        validate_chunk([row], 0, HOUR_MS)
    assert validate_chunk([row], 0, HOUR_MS, allow_partial=True) == [row]
    row[6] = HOUR_MS
    with pytest.raises(ValueError):
        validate_chunk([row], 0, HOUR_MS, allow_partial=True)
