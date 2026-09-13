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

from t3_engine.ai_advisor import trade_journal as journal
from t3_engine.ai_advisor.ai_trading import analysis_fingerprint, scenario_from_analysis
from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.candle_builder.aggregator import MultiTimeframeCandleBuilder, Trade
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe
from t3_engine.logger.decision_logger import DecisionLogger
from t3_engine.market_data.bybit_ws_client import BybitFuturesWebSocketClient, parse_taker_side_is_buyer_maker

logger = logging.getLogger(__name__)

# Every timeframe the live dashboard chart can display. Only 1m is
# confirmation-only now - TRADEABLE_TIMEFRAMES (common/types.py) is
# (M5, M15, H1, H4), widened from the original spec-section-21 (M5, M15)
# on the project owner's explicit instruction to enable 1h/4h trading too
# (see backtest/engine.py's _maybe_open_trade guard). Each of these still
# gets its own full BacktestEngine so its candles/structure/scenario are
# real and independently tracked, not derived/resampled from the 5m one -
# the guard in _maybe_open_trade is what stops 1m from ever opening a
# paper position, not its absence from this tuple.
DISPLAY_TIMEFRAMES = (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)


class LiveTradingEngine:
    def __init__(self, symbol: str, trading_timeframes: tuple = DISPLAY_TIMEFRAMES,
                 initial_equity: float = 10_000.0, entry_confidence_threshold: float = 75.0,
                 log_dir: str = "./logs", ai_only: bool = True):
        self.symbol = symbol
        self.trading_timeframes = trading_timeframes
        self.logger = DecisionLogger(log_dir=log_dir)
        # A live session is the AI's chart: it shows the agent's count and
        # nothing else, so it trades the agent's count and nothing else
        # (backtest/engine.py's ai_only). Default on for live; a replay or
        # a test can turn it off to exercise the engine's own path.
        self.ai_only = ai_only

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
        # How many candles per timeframe came from REST history rather than
        # from the live stream. Everything at or below this index existed
        # before the connection did - useful for saying "the analysis is 12
        # candles old" rather than leaving a saved count to look current.
        self.seeded: Dict[Timeframe, int] = {tf: 0 for tf in trading_timeframes}
        for engine in self.engines.values():
            engine.ai_only = ai_only
        # The saved AI count each timeframe is currently trading, by the
        # fingerprint that identifies it - so "has the agent changed its
        # mind" is answerable without re-deriving the count every poll.
        self.ai_counts: Dict[Timeframe, str] = {}
        # Every fill, in order, as it happened. The in-memory half of the
        # trade journal: the dashboard reads this, and trade_journal writes
        # the same events to the database so they survive a restart.
        self.fills: List[Dict] = []
        # Realized P&L already journalled per position, so each event can
        # carry ITS share rather than the running total counted again.
        self._last_realized: Dict[str, float] = {}
        for timeframe, engine in self.engines.items():
            engine.on_fill = self._make_fill_recorder(timeframe)

    def _make_fill_recorder(self, timeframe: Timeframe):
        """Bind one timeframe's fills to the journal.

        A take-profit leg filling is the moment the count was RIGHT about
        something, and a stop is the moment it was wrong. Until this
        existed both went unrecorded: a position with two of four legs
        filled read as "open: 1, closed: 0" and the agent was asked to
        label the same chart again knowing nothing about either."""
        def record(position, reason: str, price: float, candle: Candle) -> None:
            event, label = reason, None
            if reason.startswith("TP_HIT:"):
                event, label = "TP_HIT", reason.split(":", 1)[1]
            entry = {
                "event": event, "label": label, "price": price,
                "timeframe": timeframe.value, "position_id": position.position_id,
                "wave_label": position.wave_label.value if position.wave_label else None,
                "side": position.side.value,
                "quantity": position.initial_quantity if event == "ENTRY" else position.quantity,
                "position_realized_pnl": round(position.realized_pnl, 6),
                "at": candle.close_time,
                "closed": position.closed,
            }
            self.fills.append(entry)
            logger.info("[live %s] %s %s @ %s (realized %+g)", self.symbol, timeframe.value,
                        reason, price, position.realized_pnl)
            journal.record(journal.TradeEvent(
                source=self.live_source, symbol=self.symbol, timeframe=timeframe.value,
                position_id=position.position_id, event=event, label=label,
                wave_label=entry["wave_label"], side=entry["side"], price=price,
                quantity=entry["quantity"],
                # On a partial leg the position's running total is what is
                # known; the per-event share is the change since the last
                # recorded event for this position.
                realized_pnl=round(position.realized_pnl - self._last_realized.get(
                    position.position_id, 0.0), 6),
                position_realized_pnl=round(position.realized_pnl, 6),
                equity=self.engines[timeframe].risk_manager.equity,
                count_fingerprint=self.ai_counts.get(timeframe, ""),
                at=candle.close_time,
            ))
            self._last_realized[position.position_id] = position.realized_pnl
        return record

    def apply_ai_analysis(self, timeframe: Timeframe, analysis) -> bool:
        """Hand this timeframe's engine the agent's current count.

        Idempotent by fingerprint: the live chart is polled every few
        seconds and re-installing an unchanged count on every poll would
        re-evaluate the same entry over and over. Returns whether anything
        actually changed.

        Nothing here is retroactive. The count takes effect for candles
        that close AFTER it arrives - trading it back over the history it
        was derived from would be lookahead of the plainest kind, since the
        agent saw that whole history before naming the waves."""
        engine = self.engines.get(timeframe)
        if engine is None:
            return False
        fingerprint = analysis_fingerprint(analysis)
        if fingerprint == self.ai_counts.get(timeframe, ""):
            return False
        scenario = scenario_from_analysis(analysis, timeframe) if analysis else None
        changed = engine.set_ai_scenario(scenario, fingerprint)
        if changed:
            self.ai_counts[timeframe] = fingerprint
            logger.info("[live %s] %s now trading the agent's count: %s", self.symbol,
                        timeframe.value,
                        " ".join(w.label.value for w in scenario.waves) if scenario else "none")
        return changed

    def seed_history(self, timeframe: Timeframe, candles: List[Candle]) -> int:
        """Replay past candles through the engine before the stream starts.

        Without this a live session begins with an empty chart and no past:
        pivots, the confirmed chain and every scenario have to be
        rediscovered from candles that arrive after connecting, so the
        first useful count is hours away on 1h and days away on 4h. The
        candles are fed through the SAME path a live one takes, so nothing
        downstream can tell the difference and the no-lookahead guarantee
        holds exactly as before - each candle is processed knowing only the
        ones before it.

        Idempotent by timestamp: re-seeding after a reconnect skips
        anything already present rather than counting it twice."""
        history = self.history[timeframe]
        known = {candle.open_time for candle in history}
        added = 0
        for candle in candles:
            if not candle.closed or candle.open_time in known:
                continue
            if history and candle.open_time <= history[-1].open_time:
                continue        # out of order: a gap is better than a lie
            self._on_candle_closed(timeframe, candle)
            added += 1
        self.seeded[timeframe] += added
        if added:
            logger.info("[live %s] seeded %d %s candles from history", self.symbol, added,
                        timeframe.value)
        return added

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
