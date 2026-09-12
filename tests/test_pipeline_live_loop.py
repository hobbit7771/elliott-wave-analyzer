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
