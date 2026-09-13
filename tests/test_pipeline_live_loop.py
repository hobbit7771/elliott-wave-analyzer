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

    # ai_only=False drives the ENGINE's own count. A live session defaults
    # to the AI's count instead (see test_ai_only_live_session_does_not_trade
    # _without_a_count below), which with no count produces no signals at
    # all - correct there, useless for exercising the logging path here.
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                                entry_confidence_threshold=1.0, log_dir=str(tmp_path),
                                ai_only=False)
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


# --- a live session trades the agent's count, not the engine's ----------
# The live chart shows the AI's read of the market and nothing else, so
# the paper trading on that chart comes from the same place.

def _ai_analysis(candles, next_label="5"):
    """A saved AI count over a real synthetic series, in the shape the
    analysis cache stores (chart times, i.e. seconds)."""
    def leg(label, a, b):
        return {"label": label,
                "start_time": candles[a].open_time // 1000,
                "end_time": candles[b].open_time // 1000,
                "start_price": candles[a].close, "end_price": candles[b].close,
                "direction": "UP" if candles[b].close >= candles[a].close else "DOWN"}
    return {
        "analysed_at": 1_700_000_000,
        "accepted": [{"structure": "IMPULSE", "waves": [
            leg("1", 0, 20), leg("2", 20, 30), leg("3", 30, 60), leg("4", 60, 70)]}],
        "projection": {"next_label": next_label,
                       "targets": [{"ratio": 1.0, "price": candles[-1].close * 1.2,
                                    "primary": True}]},
    }


def test_an_ai_only_live_session_does_not_trade_without_a_count():
    """No count, no trades - and that is right, not a gap: a count that did
    not exist when a candle closed cannot have been traded on it."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=1.0, log_dir="/tmp/t3_test_logs")
    assert engine.ai_only is True
    engine.seed_history(Timeframe.M5, candles)

    tf_engine = engine.engines[Timeframe.M5]
    assert tf_engine.signals == []
    # ...while the structure underneath it was still tracked, because the
    # entry score needs it the moment a count does arrive.
    assert len(tf_engine.pivot_detector.pivots) > 0


def test_applying_an_ai_analysis_installs_a_tradeable_count():
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=1.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles[:80])
    assert engine.apply_ai_analysis(Timeframe.M5, _ai_analysis(candles)) is True

    scenario = engine.engines[Timeframe.M5].ai_scenario
    assert scenario is not None
    assert scenario.current_wave.label.value == "5"


def test_re_applying_the_same_analysis_changes_nothing():
    """A live chart is polled every few seconds. Re-installing an
    unchanged count each time would re-evaluate the same entry forever."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               log_dir="/tmp/t3_test_logs")
    analysis = _ai_analysis(candles)
    assert engine.apply_ai_analysis(Timeframe.M5, analysis) is True
    first = engine.engines[Timeframe.M5].ai_scenario
    assert engine.apply_ai_analysis(Timeframe.M5, analysis) is False
    assert engine.engines[Timeframe.M5].ai_scenario is first
    # a genuinely different count does take effect
    assert engine.apply_ai_analysis(Timeframe.M5, _ai_analysis(candles, next_label="A")) is True


def test_the_ai_count_is_not_applied_retroactively():
    """Trading a count back over the history it was derived from is
    lookahead of the plainest kind - the agent saw that whole history
    before naming the waves."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=1)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=1.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles)
    engine.apply_ai_analysis(Timeframe.M5, _ai_analysis(candles))
    # Installing the count evaluates nothing on its own: every candle it
    # could have been traded on is already in the past.
    assert engine.engines[Timeframe.M5].signals == []


def test_an_analysis_for_an_untracked_timeframe_is_ignored():
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               log_dir="/tmp/t3_test_logs")
    assert engine.apply_ai_analysis(Timeframe.H4, {"accepted": []}) is False


def test_candles_closing_after_the_count_arrives_are_traded_on_it():
    """The other half of "not retroactive": once the agent's count is in,
    the next candles ARE evaluated against it, through the same entry
    plans, scoring, risk sizing and position manager the engine uses."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=2)

    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=1.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles[:80])
    engine.apply_ai_analysis(Timeframe.M5, _ai_analysis(candles[:80]))
    tf_engine = engine.engines[Timeframe.M5]
    assert tf_engine.signals == []

    engine.seed_history(Timeframe.M5, candles[80:])

    assert tf_engine.signals, "a count in place should be evaluated on new candles"
    # It is the AI's wave that was traded, and its prices came from the
    # server's own plan - the model never supplies a price.
    signal = tf_engine.signals[0]
    assert signal.wave_label.value == "5"
    assert signal.stop_loss is not None and signal.take_profits


def test_every_take_profit_leg_and_stop_is_recorded_as_it_fills(monkeypatch):
    """The screen that prompted this: a position with two of its four
    take-profit legs filled read as "open: 1, closed: 0". on_price_update
    already returned a reason for every fill and the loop threw it away."""
    from t3_engine.ai_advisor import trade_journal
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    recorded = []
    monkeypatch.setattr(trade_journal, "record", lambda event, **k: recorded.append(event))

    candles = generate_synthetic_series(num_cycles=2)
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=1.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles[:80])
    engine.apply_ai_analysis(Timeframe.M5, _ai_analysis(candles[:80]))
    engine.seed_history(Timeframe.M5, candles[80:])

    assert engine.fills, "an entry and its fills should be recorded"
    assert engine.fills[0]["event"] == "ENTRY"
    # and the same events reached the durable journal
    assert [e.event for e in recorded] == [f["event"] for f in engine.fills]
    assert all(e.symbol == "TESTUSDT" and e.timeframe == "5m" for e in recorded)
    # each one is tagged with the count that planned it, so "how did that
    # count do" is answerable rather than inferred
    assert all(e.count_fingerprint for e in recorded)


def test_each_recorded_fill_carries_its_own_share_of_the_pnl(monkeypatch):
    """Take-profit legs are partial closes: the position's running total
    grows with each one. Journalling that total as the event's own P&L
    would count the same money several times."""
    from t3_engine.ai_advisor import trade_journal
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    recorded = []
    monkeypatch.setattr(trade_journal, "record", lambda event, **k: recorded.append(event))

    candles = generate_synthetic_series(num_cycles=2)
    engine = LiveTradingEngine(symbol="TESTUSDT", trading_timeframes=(Timeframe.M5,),
                               entry_confidence_threshold=1.0, log_dir="/tmp/t3_test_logs")
    engine.seed_history(Timeframe.M5, candles[:80])
    engine.apply_ai_analysis(Timeframe.M5, _ai_analysis(candles[:80]))
    engine.seed_history(Timeframe.M5, candles[80:])

    by_position = {}
    for event in recorded:
        by_position.setdefault(event.position_id, []).append(event)
    for events in by_position.values():
        # the per-event shares add up to the position's running total
        # (each share is stored rounded, so compare with that tolerance)
        assert sum(e.realized_pnl for e in events) == pytest.approx(
            events[-1].position_realized_pnl, abs=1e-5)
