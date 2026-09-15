"""One symbol's world: every feature module, and the snapshot they produce.

This is where the pieces meet. Each module here owns one question - the
book, the flow, the CVD, the liquidations, the structure - and `SymbolState`
holds one of each, routes incoming messages to the right one, and assembles
the answer.

Two details are load-bearing:

  Trades are handed to the ORDER BOOK as well as to the flow modules
  (`OrderBook.note_trade`). That is what lets the book tell a filled order
  from a cancelled one; without it the pulling metric reads every
  execution as a withdrawal, and pulling is one of the heaviest features
  in the pre-break score.

  The snapshot is built on a clock, not on every message. The streams
  deliver hundreds of updates a second and nothing downstream can use
  them at that rate, so `snapshot()` recomputes at most every
  `recompute_interval_seconds` and returns the cached frame in between.
  A caller that genuinely needs a fresh frame passes force=True.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from t3_engine.lead_engine import cvd as cvd_module
from t3_engine.lead_engine.btc_leadlag import BtcLeadLag
from t3_engine.lead_engine.candles import CandleBuilder
from t3_engine.lead_engine.config import LeadEngineConfig, Thresholds
from t3_engine.lead_engine.elliott_state import ElliottContext
from t3_engine.lead_engine.health import StreamHealth, assess
from t3_engine.lead_engine.liquidation_engine import LiquidationEngine, Liquidation
from t3_engine.lead_engine.microprice import MicropriceState
from t3_engine.lead_engine.oi_engine import OpenInterestState
from t3_engine.lead_engine.orderbook_engine import OrderBook
from t3_engine.lead_engine.prebreak_engine import (
    LONG,
    PreBreakEngine,
    PreBreakInputs,
    SHORT,
)
from t3_engine.lead_engine.pressure_engine import describe, score as pressure_score
from t3_engine.lead_engine.signal_machine import SignalInputs, SignalMachine
from t3_engine.lead_engine.smc_engine import Candle, SmcEngine
from t3_engine.lead_engine.trade_flow import Trade, TradeFlow

# The interval the structure modules read. One minute is the compromise
# the whole tab is built around: fine enough that a level identified on it
# is still relevant to the next few minutes, coarse enough that its swings
# are not noise. The other intervals are subscribed and stored for the UI,
# but the features are computed from this one.
STRUCTURE_INTERVAL = "1"

# Levels, though, are tracked on a much finer series. The structure
# context answers "what is this market doing"; the level tracker answers
# "which price is under pressure in the next few minutes", and at one
# minute a ten-minute capture holds ten bars - too few for a fractal swing
# to exist at all, so no level is ever found and the pre-break score stays
# at zero however clear the order flow is. Found by replay. Fifteen
# seconds is the right granularity for a horizon measured in minutes.
LEVEL_INTERVAL_MS = 15_000


@dataclass
class SymbolState:
    symbol: str
    config: LeadEngineConfig
    lead_lag: BtcLeadLag
    thresholds: Thresholds = field(default_factory=Thresholds)

    def __post_init__(self) -> None:
        self.book = OrderBook(self.symbol, self.config.orderbook_depth, self.thresholds)
        self.flow = TradeFlow(self.symbol, self.thresholds)
        self.cvd = cvd_module.CvdState(self.symbol)
        self.microprice = MicropriceState(self.symbol)
        self.liquidations = LiquidationEngine(self.symbol, self.thresholds)
        self.open_interest = OpenInterestState(self.symbol)
        self.smc: Dict[str, SmcEngine] = {}
        # Structure from locally-built bars as well as from the exchange's
        # klines - see candles.py. Without this the engine has no swings,
        # and therefore no levels and no pre-break score, for the first
        # several minutes of every process.
        self.local_candles = CandleBuilder(self.symbol)
        self.local_smc = SmcEngine(self.symbol, f"{STRUCTURE_INTERVAL}-local")
        self.level_candles = CandleBuilder(self.symbol, LEVEL_INTERVAL_MS)
        self.elliott = ElliottContext(self.symbol, STRUCTURE_INTERVAL)
        self.prebreak = PreBreakEngine(self.symbol, self.thresholds)
        self.signals = SignalMachine(self.symbol, self.thresholds)
        self.health = StreamHealth(self.symbol)
        self.last_price: Optional[float] = None
        self.ticker: Dict[str, Any] = {}
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_at = 0.0
        self.feature_history: List[Dict[str, Any]] = []

    # ---- ingest ----

    def on_orderbook(self, message: Dict[str, Any]) -> bool:
        applied = self.book.apply(message)
        stamp = int(message.get("ts") or 0)
        if stamp:
            self.health.last_book_ms = stamp
        self.health.orderbook_synced = self.book.synced
        if not applied and str(message.get("type", "")).lower() == "delta":
            self.health.dropped_messages = self.book.gaps
        if applied and self.book.synced:
            self.microprice.observe(stamp or self.health.last_book_ms,
                                    self.book.microprice(), self.book.midpoint())
            metrics = self.book.metrics()
            self.prebreak.observe_depth(stamp or self.health.last_book_ms,
                                        metrics.bid_depth, metrics.ask_depth)
        return applied

    def on_trade(self, trade: Trade) -> None:
        self.flow.add(trade)
        self.cvd.add(trade)
        # The book needs to know this was EXECUTED, not cancelled. See the
        # module docstring; this one line is what separates pulling from
        # normal trading.
        self.book.note_trade("Buy" if trade.is_taker_buy else "Sell", trade.quantity)
        self.last_price = trade.price
        self.health.last_trade_ms = trade.timestamp_ms
        self.lead_lag.observe(self.symbol, trade.timestamp_ms, trade.price)
        closed = self.local_candles.add(trade)
        if closed is not None:
            self.local_smc.update(closed)
            self.elliott.update(closed)
        self.level_candles.add(trade)

    def on_ticker(self, data: Dict[str, Any], stamp_ms: int) -> None:
        self.ticker.update({k: v for k, v in data.items() if v is not None})
        self.health.last_ticker_ms = stamp_ms
        price = data.get("lastPrice") or data.get("markPrice")
        try:
            if price is not None:
                self.last_price = float(price)
        except (TypeError, ValueError):
            pass

    def on_liquidation(self, event: Liquidation) -> None:
        self.liquidations.add(event)
        self.health.last_liquidation_ms = event.timestamp_ms

    def on_kline(self, interval: str, candle: Candle) -> None:
        engine = self.smc.get(interval)
        if engine is None:
            engine = SmcEngine(self.symbol, interval)
            self.smc[interval] = engine
        engine.update(candle)
        if interval == STRUCTURE_INTERVAL:
            self.elliott.update(candle)
        self.health.last_kline_ms = max(self.health.last_kline_ms, candle.start_ms)

    def on_open_interest(self, value: float, stamp_ms: int) -> None:
        self.open_interest.observe(stamp_ms, value, self.last_price)
        self.open_interest.polls += 1
        self.health.last_oi_ms = stamp_ms

    # ---- assembly ----

    def _structure_engine(self) -> Optional[SmcEngine]:
        """Whichever structure series has more CLOSED bars to count.

        The exchange's klines win once they have accumulated - they carry
        history from before this process connected - and the locally built
        bars carry the engine until then."""
        exchange = self.smc.get(STRUCTURE_INTERVAL) or next(iter(self.smc.values()), None)
        local_closed = sum(1 for c in self.local_smc.candles if c.closed)
        if exchange is None:
            return self.local_smc if local_closed else None
        exchange_closed = sum(1 for c in exchange.candles if c.closed)
        return exchange if exchange_closed >= local_closed else self.local_smc

    def components(self) -> Dict[str, Optional[float]]:
        """The nine pressure inputs.

        None where a stream has not produced enough to answer - see
        pressure_engine.score for why None is not zero."""
        structure = self._structure_engine()
        book_component = self.book.pressure_component() if self.book.synced else None
        micro_component = (self.microprice.pressure_component()
                           if len(self.microprice.series) >= 2 else None)
        flow_component = self.flow.pressure_component() if len(self.flow.trades) else None
        velocity_component = self.flow.velocity_component() if len(self.flow.trades) else None
        liquidity_component = self._liquidity_component()
        liquidation_component = (self.liquidations.pressure_component()
                                 if len(self.liquidations.events) else None)
        oi_component = (self.open_interest.pressure_component()
                        if len(self.open_interest.series) >= 2 else None)
        lead_component = self.lead_lag.pressure_component(self.symbol)
        smc_component = structure.pressure_component() if structure else None
        elliott_component = self.elliott.pressure_component() if self.elliott.candles else None

        # Open interest is not one of the nine named weights; the
        # specification folds positioning into the liquidation view. It is
        # blended into the liquidation component rather than given a
        # weight of its own, so the published weights still sum to one.
        if liquidation_component is not None and oi_component is not None:
            liquidation_component = 0.7 * liquidation_component + 0.3 * oi_component
        elif liquidation_component is None and oi_component is not None:
            liquidation_component = 0.5 * oi_component

        return {
            "order_book_imbalance": book_component,
            "microprice": micro_component,
            "cvd": self.cvd.pressure_component() if len(self.cvd.series) else None,
            "trade_velocity": velocity_component,
            "liquidity_shift": liquidity_component,
            "liquidations": liquidation_component,
            "btc_lead_lag": lead_component,
            "smc": smc_component,
            "elliott_context": elliott_component,
            # kept out of the weighted set, reported for the UI
            "_flow": flow_component,
        }

    def _liquidity_component(self) -> Optional[float]:
        """Pulling and replenishment, netted into one signed number.

        Bids being refilled while offers are pulled is upward pressure;
        the reverse is downward. Expressed as a share so that a fast
        instrument and a slow one land on the same scale."""
        if not self.book.synced:
            return None
        metrics = self.book.metrics()
        bid_side = metrics.bid_replenishment - metrics.bid_pulling
        ask_side = metrics.ask_replenishment - metrics.ask_pulling
        total = abs(bid_side) + abs(ask_side)
        if total <= 0:
            return 0.0
        return max(-1.0, min(1.0, (bid_side - ask_side) / total))

    def snapshot(self, force: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        if not force and self._snapshot is not None and \
                (now - self._snapshot_at) < self.config.recompute_interval_seconds:
            return self._snapshot

        book_metrics = self.book.metrics()
        components = self.components()
        flow_component = components.pop("_flow", None) or 0.0
        pressure = pressure_score(components, self.config.weights)

        structure = self._structure_engine()
        smc_state = structure.state().as_dict() if structure else {}
        lead = self.lead_lag.as_dict(self.symbol)

        price = self.last_price or book_metrics.midpoint or 0.0
        # The level tracker reads the fine series; the SMC block above
        # reads the minute one. Two different questions - see
        # LEVEL_INTERVAL_MS.
        candles = self.level_candles.series()
        if not candles and structure is not None:
            candles = structure.candles
        prebreak_inputs = PreBreakInputs(
            price=price,
            book=book_metrics,
            microprice_offset_bps=self.microprice.offset_bps(),
            cvd_component=components.get("cvd") or 0.0,
            flow_component=flow_component,
            velocity_zscore=self.flow.velocity_zscore(),
            velocity_acceleration=self.flow.acceleration(),
            btc_lead_score=float(lead.get("btc_lead_score") or 0.0),
            candles=candles,
        )
        long_break = self.prebreak.evaluate(LONG, prebreak_inputs)
        short_break = self.prebreak.evaluate(SHORT, prebreak_inputs)

        now_ms = int(now * 1000)
        verdict = assess(self.health, self.thresholds, now_ms=now_ms, started=True)
        signal = self.signals.update(SignalInputs(
            long_pressure=pressure.long_pressure,
            short_pressure=pressure.short_pressure,
            conflict=pressure.conflict,
            prebreak_long=long_break.probability,
            prebreak_short=short_break.probability,
            long_level=long_break.level,
            short_level=short_break.level,
            liquidation_state=self.liquidations.state(),
            healthy=verdict.signals_enabled,
            health_reason="; ".join(verdict.reasons),
        ), now=now)

        frame = {
            "symbol": self.symbol,
            "generated_at": now,
            "price": price,
            "ticker": dict(self.ticker),
            "orderbook": book_metrics.as_dict(),
            "microprice": self.microprice.as_dict(),
            "trade_flow": self.flow.as_dict(),
            "cvd": self.cvd.as_dict(),
            "liquidations": self.liquidations.as_dict(),
            "open_interest": self.open_interest.as_dict(),
            "btc_lead": lead,
            "smc": smc_state,
            "elliott": self.elliott.as_dict(),
            "pressure": {**pressure.as_dict(), "explanation": describe(pressure)},
            "prebreak": {"long": long_break.as_dict(), "short": short_break.as_dict()},
            "signal": signal.as_dict(),
            "health": {**self.health.as_dict(now_ms), **verdict.as_dict()},
        }
        self._snapshot = frame
        self._snapshot_at = now
        self._remember(frame)
        return frame

    def _remember(self, frame: Dict[str, Any]) -> None:
        """A thin feature row per frame, bounded, for the history endpoint
        and for anything that wants to see how a signal built up."""
        self.feature_history.append({
            "t": frame["generated_at"], "price": frame["price"],
            "long_pressure": frame["pressure"]["long_pressure"],
            "short_pressure": frame["pressure"]["short_pressure"],
            "obi5": frame["orderbook"].get("obi", {}).get("obi5"),
            "cvd": frame["cvd"].get("cvd"),
            "prebreak_long": frame["prebreak"]["long"]["break_probability"],
            "prebreak_short": frame["prebreak"]["short"]["break_probability"],
            "state": frame["signal"]["state"],
        })
        if len(self.feature_history) > self.config.max_feature_snapshots:
            self.feature_history.pop(0)
