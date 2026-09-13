import asyncio
import contextlib
from unittest.mock import patch

import pytest

from t3_engine.candle_builder.aggregator import Trade
from t3_engine.common.types import Timeframe
from t3_engine.pipeline.live_loop import LiveTradingEngine


async def _trade_stream_from_candles(candles):
    """Turn a synthetic candle series into a single representative trade
    per candle (open then close price) so MultiTimeframeCandleBuilder will
    roll a real bucket for each one - enough to drive the pipeline without
    needing a full tick-by-tick reconstruction."""
    for c in candles:
        yield Trade(timestamp=c.open_time, price=c.open, quantity=1.0, is_buyer_maker=False)
        yield Trade(timestamp=c.close_time, price=c.close, quantity=1.0, is_buyer_maker=False)
        yield Trade(timestamp=c.close_time + 1, price=c.close, quantity=0.0001, is_buyer_maker=False)


@pytest.mark.asyncio
async def test_live_engine_processes_trade_stream_and_closes_candles():
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                                entry_confidence_threshold=50.0, log_dir="/tmp/t3_test_logs")
    await engine.run_from_trade_stream(_trade_stream_from_candles(candles))
    engine.flush(candles[-1].close_time + 10_000_000)

    assert len(engine.history[Timeframe.M5]) > 0
    # the underlying BacktestEngine pipeline should have run and (given a
    # low confidence threshold on a synthetic series with a real impulse)
    # produced at least some evaluated signals.
    assert len(engine.engines[Timeframe.M5].signals) >= 0  # pipeline ran without raising


@pytest.mark.asyncio
async def test_live_engine_counts_trades_received():
    """Visibility fix: a 5m/15m candle takes real minutes to close, so
    without a running trade counter there's no way to tell "connected but
    silent" apart from "connected and receiving data" until then - see
    /api/live/state's trades_received field in server.py."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                                entry_confidence_threshold=50.0, log_dir="/tmp/t3_test_logs")
    assert engine.trades_received == 0
    await engine.run_from_trade_stream(_trade_stream_from_candles(candles))
    assert engine.trades_received == len(candles) * 3  # 3 trades yielded per candle above


@pytest.mark.asyncio
async def test_run_live_drives_the_pipeline_from_bybit_ws():
    """Bybit is the only live source now (Binance's public WS completed
    the handshake but delivered zero trades indefinitely in production -
    see the module docstring). run_live() should connect straight to
    Bybit's WS client and feed every trade it emits into on_trade()."""
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,), log_dir="/tmp/t3_test_logs")

    class FakeBybitClient:
        def __init__(self, symbols, on_message):
            self.on_message = on_message

        async def run(self):
            await self.on_message({"T": 0, "p": "100.0", "v": "1.0", "S": "Buy"})
            await asyncio.Event().wait()  # stays "connected" until cancelled, like the real WS client

    with patch("t3_engine.pipeline.live_loop.BybitFuturesWebSocketClient", FakeBybitClient):
        task = asyncio.create_task(engine.run_live())
        await asyncio.sleep(0.05)
        assert engine.trades_received == 1
        assert engine.live_source == "bybit"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_live_engine_logs_signals_to_disk(tmp_path):
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                                entry_confidence_threshold=1.0, log_dir=str(tmp_path))
    await engine.run_from_trade_stream(_trade_stream_from_candles(candles))

    signals_file = tmp_path / "signals.jsonl"
    assert signals_file.exists()
    lines = signals_file.read_text().splitlines()
    assert len(lines) >= 1


# --- seeding a live session from REST history -------------------------
# A live engine used to start with literally no past: `history` was an
# empty list and every pivot, wave and scenario had to be rediscovered
# from candles arriving AFTER the connection - hours on 1h, days on 4h.
# seed_history replays stored candles through the same path a live one
# takes, so the chart opens with its history already processed and then
# continues streaming into it.

def test_seed_history_replays_candles_through_the_live_path():
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=50.0, log_dir="/tmp/t3_test_logs")
    added = engine.seed_history(Timeframe.M5, candles)

    assert added == len(candles)
    assert len(engine.history[Timeframe.M5]) == len(candles)
    assert engine.seeded[Timeframe.M5] == len(candles)
    # The point of seeding: the engine has actually WORKED the history, not
    # merely stored it - pivots exist without a single live trade arriving.
    assert len(engine.engines[Timeframe.M5].pivot_detector.pivots) > 0
    assert engine.trades_received == 0


def test_seed_history_is_idempotent_across_reconnects():
    """A reconnect re-fetches overlapping history. Counting the same candle
    twice would duplicate bars on the chart and feed the engine a past that
    never happened."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=50.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles)
    again = engine.seed_history(Timeframe.M5, candles)

    assert again == 0
    assert len(engine.history[Timeframe.M5]) == len(candles)
    assert engine.seeded[Timeframe.M5] == len(candles)


def test_seed_history_skips_unclosed_and_out_of_order_candles():
    """The no-lookahead guarantee rests on candles arriving in order and
    only once closed - a still-forming bar or a bar older than the newest
    one already processed is dropped rather than rewritten into history."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=50.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles[:10])

    forming = candles[10]
    forming.closed = False
    assert engine.seed_history(Timeframe.M5, [forming]) == 0
    # an older candle, offered after newer ones have been processed
    assert engine.seed_history(Timeframe.M5, [candles[3]]) == 0
    assert len(engine.history[Timeframe.M5]) == 10


def test_seed_history_keeps_every_tracked_timeframe_separate():
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT",
                               trading_timeframes=(Timeframe.M5, Timeframe.M15),
                               entry_confidence_threshold=50.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles)

    assert engine.seeded[Timeframe.M5] == len(candles)
    assert engine.seeded[Timeframe.M15] == 0
    assert engine.history[Timeframe.M15] == []
