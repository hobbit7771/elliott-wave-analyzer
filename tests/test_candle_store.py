"""The candle series an analysis was run on, kept with it.

The analysis cache stores a count - which swings are waves 1 through 5 -
and not the chart those waves sit on. A count that cannot be re-checked
against the exact bars it was made on is a claim about a window nobody has
any more.
"""

import pytest

from t3_engine.ai_advisor import analysis_store
from t3_engine.backtest.synthetic_data import generate_synthetic_series_for
from t3_engine.common.types import Timeframe
from t3_engine.database import candle_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'candles.db'}"
    monkeypatch.setattr(analysis_store, "DEFAULT_DATABASE_URL", url)
    monkeypatch.setattr(analysis_store, "_factory", None)
    monkeypatch.setattr(candle_store, "_factory", None)
    return url


def test_a_series_survives_the_round_trip_unchanged(store):
    """Every field, not just the closes: a count is checked against highs
    and lows, and volume is what the volume tool reads."""
    candles = generate_synthetic_series_for(Timeframe.M15, num_cycles=1)
    written = candle_store.save("INJUSDT", "15m", candles, database_url=store)
    assert written == len(candles)

    back = candle_store.load("INJUSDT", "15m", database_url=store)
    assert len(back) == len(candles)
    for original, restored in zip(candles, back):
        assert restored.open_time == original.open_time
        assert restored.close_time == original.close_time
        assert (restored.open, restored.high, restored.low, restored.close) == \
               (original.open, original.high, original.low, original.close)
        assert restored.volume == original.volume
        assert restored.taker_buy_volume == original.taker_buy_volume
        assert restored.timeframe == Timeframe.M15


def test_candles_come_back_oldest_first(store):
    """Every consumer of a candle list in this codebase assumes that
    order; pivots computed on a reversed series are nonsense."""
    candles = generate_synthetic_series_for(Timeframe.H1, num_cycles=1)
    candle_store.save("INJUSDT", "1h", candles, database_url=store)
    back = candle_store.load("INJUSDT", "1h", database_url=store)
    assert [c.open_time for c in back] == sorted(c.open_time for c in back)
    assert back[0].open_time == candles[0].open_time


def test_saving_replaces_rather_than_appends(store):
    """Two overlapping windows of the same instrument invite reading the
    wrong one - the same reasoning as the analysis cache."""
    first = generate_synthetic_series_for(Timeframe.M5, num_cycles=1)
    candle_store.save("INJUSDT", "5m", first, database_url=store)
    second = generate_synthetic_series_for(Timeframe.M5, num_cycles=2)
    candle_store.save("INJUSDT", "5m", second, database_url=store)

    back = candle_store.load("INJUSDT", "5m", database_url=store)
    assert len(back) == len(second)


def test_timeframes_and_symbols_are_kept_apart(store):
    m5 = generate_synthetic_series_for(Timeframe.M5, num_cycles=1)
    h4 = generate_synthetic_series_for(Timeframe.H4, num_cycles=1)
    candle_store.save("INJUSDT", "5m", m5, database_url=store)
    candle_store.save("INJUSDT", "4h", h4, database_url=store)
    candle_store.save("BTCUSDT", "5m", h4, database_url=store)

    assert candle_store.load("INJUSDT", "5m", database_url=store)[0].timeframe == Timeframe.M5
    assert candle_store.load("INJUSDT", "4h", database_url=store)[0].timeframe == Timeframe.H4
    assert len(candle_store.load("BTCUSDT", "5m", database_url=store)) == len(h4)
    assert candle_store.load("NOSUCHUSDT", "5m", database_url=store) == []


def test_a_read_is_bounded(store):
    candles = generate_synthetic_series_for(Timeframe.M5, num_cycles=2)
    candle_store.save("INJUSDT", "5m", candles, database_url=store)
    assert len(candle_store.load("INJUSDT", "5m", limit=10, database_url=store)) == 10


def test_an_unknown_timeframe_reads_as_empty_rather_than_raising(store):
    assert candle_store.load("INJUSDT", "37m", database_url=store) == []


def test_storage_failure_never_propagates_into_the_analysis(monkeypatch):
    """Losing the copy of the candles is a small loss; losing the ANALYSIS
    because its candles could not be filed would be a much worse trade."""
    def explode(*a, **k):
        raise RuntimeError("database is gone")
    monkeypatch.setattr(candle_store, "_sessions", explode)
    candles = generate_synthetic_series_for(Timeframe.M5, num_cycles=1)
    assert candle_store.save("INJUSDT", "5m", candles) == 0
    assert candle_store.load("INJUSDT", "5m") == []


def test_an_empty_series_is_not_written(store):
    assert candle_store.save("INJUSDT", "5m", [], database_url=store) == 0
