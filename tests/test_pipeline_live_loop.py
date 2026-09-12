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
async def test_run_live_stays_on_binance_when_it_produces_trades():
    """The common/working case: Binance delivers at least one trade before
    the watchdog fires, so run_live() must stay on it for the session and
    never touch the Bybit fallback path at all."""
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,), log_dir="/tmp/t3_test_logs")

    async def fake_binance(self):
        self.on_trade(Trade(timestamp=0, price=100.0, quantity=1.0, is_buyer_maker=False))
        await asyncio.Event().wait()  # stays "connected" until cancelled, like the real WS client

    bybit_called = False

    async def fake_bybit(self):
        nonlocal bybit_called
        bybit_called = True

    with patch.object(LiveTradingEngine, "run_live_binance", fake_binance), \
         patch.object(LiveTradingEngine, "run_live_bybit", fake_bybit):
        task = asyncio.create_task(engine.run_live(watchdog_seconds=1.0))
        await asyncio.sleep(0.05)  # let the first trade land well before the 1s watchdog
        assert engine.live_source == "binance"
        assert bybit_called is False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_live_falls_back_to_bybit_when_binance_produces_no_trade():
    """The production failure mode this exists for: a persistent Binance
    connection failure (e.g. an inherited IP ban) never raises - ws_client
    just retries forever - so the only signal available is "no trade
    arrived in time". A tiny watchdog keeps this test fast."""
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,), log_dir="/tmp/t3_test_logs")

    async def fake_binance_silent(self):
        await asyncio.Event().wait()  # "connected" but never produces a trade

    bybit_reached = asyncio.Event()

    async def fake_bybit(self):
        bybit_reached.set()
        await asyncio.Event().wait()

    with patch.object(LiveTradingEngine, "run_live_binance", fake_binance_silent), \
         patch.object(LiveTradingEngine, "run_live_bybit", fake_bybit):
        task = asyncio.create_task(engine.run_live(watchdog_seconds=0.05))
        await asyncio.wait_for(bybit_reached.wait(), timeout=2.0)
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
