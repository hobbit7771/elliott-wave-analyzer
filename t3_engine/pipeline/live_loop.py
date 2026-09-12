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

NETWORK NOTE: `run_live_binance()` is real, documented Binance combined-
stream wiring (see market_data/ws_client.py), but this sandboxed build
session cannot open a socket to `fstream.binance.com` (outbound blocked at
the proxy - see README for the exact error). `run_from_trade_stream()` is
what is actually exercised in tests here, fed by a fake async trade
generator - it drives the identical processing path `run_live_binance`
would, so nothing about the pipeline logic itself is unverified, only the
live socket connection.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, List

from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.candle_builder.aggregator import MultiTimeframeCandleBuilder, Trade
from t3_engine.common.models import Candle
from t3_engine.common.types import TRADEABLE_TIMEFRAMES, Timeframe
from t3_engine.logger.decision_logger import DecisionLogger
from t3_engine.market_data.ws_client import BinanceFuturesWebSocketClient

logger = logging.getLogger(__name__)


class LiveTradingEngine:
    def __init__(self, symbol: str, trading_timeframes: tuple = TRADEABLE_TIMEFRAMES,
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
        generator. This is the method under test; `run_live_binance` below
        is a thin, real, but currently network-unverifiable adapter on
        top of it."""
        async for trade in trade_stream:
            self.on_trade(trade)

    async def run_live_binance(self) -> None:
        async def on_message(stream: str, data: dict) -> None:
            if data.get("e") != "aggTrade":
                return
            self.on_trade(Trade(
                timestamp=int(data["T"]), price=float(data["p"]), quantity=float(data["q"]),
                is_buyer_maker=bool(data["m"]),
            ))

        logger.info("[live %s] connecting to Binance public WebSocket...", self.symbol)
        ws_client = BinanceFuturesWebSocketClient(
            symbols=[self.symbol], streams=["aggTrade", "bookTicker", "markPrice"], on_message=on_message,
        )
        await ws_client.run()
