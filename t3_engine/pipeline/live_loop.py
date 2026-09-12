"""Event-driven real-time orchestrator (spec section 20).

Pipeline, exactly as specified:

    market event (trade)
    -> update tick state / candle builder
    -> update second/minute/5m/15m candles
    -> update market structure
    -> update Elliott scenarios
    -> update probability
    -> update trade state
    -> evaluate existing position
    -> evaluate new entry
    -> execute/manage order
    -> log state

No `while True: REST; sleep()` polling loop anywhere in the hot path - the
only driver is `on_trade()`, called once per incoming trade/aggTrade
message. Each TRADING_TIMEFRAME (5m/15m) gets its own independent
`BacktestEngine` instance acting as "pipeline state" - it's the same
class used for backtesting, which is precisely the point: a live run and a
backtest replay share one implementation, so there is no way for the two
to silently drift apart (section 16's whole no-repaint requirement, in
one sentence).

LIVE DATA SOURCE: Bybit's public WebSocket only (market_data/bybit_ws_client.py).
Binance was the original source, but in production its public WS
completed the connection handshake yet delivered zero trades indefinitely
(confirmed via `trades_received` staying at 0 for many minutes on a real
deploy, alongside a Binance-side IP ban already affecting its REST API -
see README) - a silent failure mode, not a real network error, so Bybit is
now the only exchange this engine actually talks to live. `market_data/
ws_client.py` (the Binance WS client) still exists and is still unit-
tested, dormant rather than deleted in case Binance access is restored
later, but nothing in this file calls it anymore.

`run_from_trade_stream()` is what is actually exercised by most tests
here, fed by a fake async trade generator - it drives the identical
processing path `run_live()` does, so nothing about the pipeline logic
itself depends on a live socket to be verified.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, List

from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.candle_builder.aggregator import MultiTimeframeCandleBuilder, Trade
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe
from t3_engine.logger.decision_logger import DecisionLogger
from t3_engine.market_data.bybit_ws_client import BybitFuturesWebSocketClient, parse_taker_side_is_buyer_maker

logger = logging.getLogger(__name__)

# Every timeframe the live dashboard chart can display (spec section 20/21:
# 1m is confirmation-only, 1h/4h are context-only, and only 5m/15m may ever
# originate a real entry - see TRADEABLE_TIMEFRAMES and the guard in
# backtest/engine.py's _maybe_open_trade). Each of these still gets its own
# full BacktestEngine so its candles/structure/scenario are real and
# independently tracked, not derived/resampled from the 5m one - the guard
# in _maybe_open_trade is what stops the non-tradeable ones from ever
# opening a paper position, not their absence from this tuple.
DISPLAY_TIMEFRAMES = (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)


class LiveTradingEngine:
    def __init__(self, symbol: str, trading_timeframes: tuple = DISPLAY_TIMEFRAMES,
                 initial_equity: float = 10_000.0, entry_confidence_threshold: float = 75.0,
                 log_dir: str = "./logs"):
        self.symbol = symbol
        self.trading_timeframes = trading_timeframes
        self.logger = DecisionLogger(log_dir=log_dir)

        self.engines: Dict[Timeframe, BacktestEngine] = {
            tf: BacktestEngine(BacktestConfig(symbol=symbol, initial_equity=initial_equity,
                                               degree=tf, entry_confidence_threshold=entry_confidence_threshold))
            for tf in trading_timeframes
        }
        self.history: Dict[Timeframe, List[Candle]] = {tf: [] for tf in trading_timeframes}
        self.candle_builder = MultiTimeframeCandleBuilder(
            list(trading_timeframes), on_closed_candle=self._on_candle_closed
        )
        # Visibility into whether real market data is actually arriving:
        # a 5m/15m candle takes 5/15 real minutes to close, so without this
        # counter there is no way to tell "connected but silent" apart from
        # "connected and receiving trades" until the first candle closes.
        self.trades_received: int = 0
        # Kept (not just a bare constant) so the dashboard's existing
        # live_source field keeps working - Bybit is the only live source
        # now, see the module docstring for why.
        self.live_source: str = "bybit"

    def on_trade(self, trade: Trade) -> None:
        self.trades_received += 1
        if self.trades_received == 1:
            logger.info("[live %s] first trade received - market data is flowing (price=%s)",
                        self.symbol, trade.price)
        elif self.trades_received % 500 == 0:
            logger.info("[live %s] %d trades received so far", self.symbol, self.trades_received)
        self.candle_builder.on_trade(trade)

    def flush(self, now_ms: int) -> None:
        self.candle_builder.flush(now_ms)

    def _on_candle_closed(self, timeframe: Timeframe, candle: Candle) -> None:
        history = self.history[timeframe]
        history.append(candle)
        engine = self.engines[timeframe]
        index = len(history) - 1
        signals_before = len(engine.signals)

        engine.process_candle(candle, index, history)

        for signal in engine.signals[signals_before:]:
            self.logger.log_signal(signal, market_context={
                "timeframe": timeframe.value,
                "trend": engine.structure.trend.value if engine.structure.trend else None,
                "equity": engine.risk_manager.equity,
                "trading_enabled": engine.risk_manager.trading_enabled,
            })
        if engine.risk_manager.disabled_reason:
            self.logger.log_event("TRADING_DISABLED", {
                "symbol": self.symbol, "timeframe": timeframe.value,
                "reason": engine.risk_manager.disabled_reason,
            })

    async def run_from_trade_stream(self, trade_stream: AsyncIterator[Trade]) -> None:
        """Drives the pipeline from any async source of trades - a live
        WS feed, a replay of stored raw_trades rows, or (in tests) a fake
        generator. This is the method under test; `run_live` below is a
        thin, real adapter on top of it, backed by Bybit's public WS."""
        async for trade in trade_stream:
            self.on_trade(trade)

    async def run_live(self) -> None:
        """Bybit-only live entry point (see module docstring for why
        Binance is no longer used here)."""
        async def on_message(item: dict) -> None:
            self.on_trade(Trade(
                timestamp=int(item["T"]), price=float(item["p"]), quantity=float(item["v"]),
                is_buyer_maker=parse_taker_side_is_buyer_maker(item.get("S", "")),
            ))

        logger.info("[live %s] connecting to Bybit public WebSocket...", self.symbol)
        ws_client = BybitFuturesWebSocketClient(symbols=[self.symbol], on_message=on_message)
        await ws_client.run()
