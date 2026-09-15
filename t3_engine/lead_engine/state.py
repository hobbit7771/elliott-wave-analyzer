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

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from t3_engine.lead_engine import cvd as cvd_module
from t3_engine.lead_engine.btc_leadlag import BtcLeadLag
from t3_engine.lead_engine.calibration import Calibrator, label_for
from t3_engine.lead_engine.candles import CandleBuilder
from t3_engine.lead_engine.config import BTC_SYMBOL, LeadEngineConfig, Thresholds
from t3_engine.lead_engine.elliott_state import ElliottContext
from t3_engine.lead_engine.health import StreamHealth, assess
from t3_engine.lead_engine.liquidation_engine import LiquidationEngine, Liquidation
from t3_engine.lead_engine.layers import (
    score_book,
    score_btc_lead,
    score_derivatives,
    score_flow,
    score_structure,
)
from t3_engine.lead_engine.microprice import MicropriceState
from t3_engine.lead_engine.normalize import Normalizer
from t3_engine.lead_engine.oi_engine import OpenInterestState
from t3_engine.lead_engine.orderbook_engine import OrderBook
from t3_engine.lead_engine.prebreak_engine import (
    LONG,
    PreBreakEngine,
    PreBreakInputs,
    SHORT,
)
from t3_engine.lead_engine.pressure_engine import score as pressure_score
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
        # Per-symbol rolling distributions. Every raw quantity that is not
        # already bounded is scaled against this instrument's own recent
        # history rather than a constant - see normalize.py.
        self.normalizer = Normalizer(self.symbol)
        # snapshot() is not a read. It pushes normalisation samples,
        # opens calibration observations and advances the signal machine,
        # and it is called from the request thread, the recorder thread
        # and replay. One at a time, or two callers double-count the same
        # tick and race each other's deques.
        self._snapshot_lock = threading.RLock()
        self.calibrator = Calibrator(self.symbol)
        self.last_price: Optional[float] = None
        self.ticker: Dict[str, Any] = {}
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_at = 0.0
        self.feature_history: List[Dict[str, Any]] = []

    # ---- ingest ----

    def on_orderbook(self, message: Dict[str, Any]) -> bool:
        applied = self.book.apply(message)
        stamp = int(message.get("ts") or 0)
        # Exchange stamp and arrival stamp, kept apart. The difference is
        # the wire; `now` minus the arrival stamp is how current THIS
        # process is. Conflating them makes a fast link carrying old data
        # indistinguishable from a slow link carrying fresh data.
        received = int(message.get("_received_at_ms") or 0) or int(time.time() * 1000)
        if stamp:
            self.health.last_book_ms = stamp
            self.health.network_latency_ms = max(0.0, float(received - stamp))
        self.health.last_book_receive_ms = received
        self.health.orderbook_synced = self.book.synced
        self.health.sequence = self.book.sequence_stats()
        if not applied and str(message.get("type", "")).lower() == "delta":
            self.health.dropped_messages = self.book.gaps
        if applied and self.book.synced:
            self.microprice.observe(stamp or self.health.last_book_ms,
                                    self.book.microprice(), self.book.midpoint())
            # The two depths, not the whole metric set. `metrics()`
            # computes OBI at five depths, weighted OBI, walls, absorption
            # and stacking; asking for all of that on every delta was the
            # largest single cost in the ingest path and it was being
            # thrown away except for two numbers.
            bid_depth, ask_depth = self.book.side_totals()
            self.prebreak.observe_depth(stamp or self.health.last_book_ms,
                                        bid_depth, ask_depth)
        return applied

    def on_trade(self, trade: Trade) -> None:
        self.flow.add(trade)
        self.cvd.add(trade)
        # The book needs to know this was EXECUTED, not cancelled. See the
        # module docstring; this one line is what separates pulling from
        # normal trading.
        self.book.note_trade("Buy" if trade.is_taker_buy else "Sell", trade.quantity,
                            trade.price)
        # Settle any calibration observation this price can now answer.
        # Only prices stamped AFTER an observation can resolve it; see
        # calibration.Calibrator.resolve.
        self.calibrator.resolve(trade.price, trade.timestamp_ms)
        self.last_price = trade.price
        self.health.last_trade_ms = trade.timestamp_ms
        self.health.last_trade_receive_ms = int(time.time() * 1000)
        self.lead_lag.observe(self.symbol, trade.timestamp_ms, trade.price)
        closed = self.local_candles.add(trade)
        if closed is not None:
            self.local_smc.update(closed)
            self.elliott.update(closed)
        self.level_candles.add(trade)

    def on_ticker(self, data: Dict[str, Any], stamp_ms: int) -> None:
        self.ticker.update({k: v for k, v in data.items() if v is not None})
        self.health.last_ticker_ms = stamp_ms
        self.health.last_ticker_receive_ms = int(time.time() * 1000)
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

    def layer_scores(self, btc_state: Optional["SymbolState"] = None) -> Dict[str, Any]:
        """The five independent readings. See layers.py.

        Replaces the flat nine-component list: a reading can now be
        attributed to a source, and two sources can be seen to disagree -
        which is what `detect_conflict` needs and what the old shape made
        impossible."""
        structure_engine = self._structure_engine()
        smc_state = structure_engine.state().as_dict() if structure_engine else {}
        elliott_component = (self.elliott.pressure_component()
                             if self.elliott.candles else None)
        lead = self.lead_lag.as_dict(self.symbol)
        price = self.last_price or self.book.midpoint()

        # BTC's own flow and book, for the lead layer to carry across.
        #
        # Read from BTC's LAST COMPUTED frame rather than re-scored here.
        # Re-scoring would push a sample into BTC's rolling normaliser
        # once per tracked symbol per tick - seven symbols, seven copies
        # of the same observation, which corrupts BTC's own z-scores -
        # and it would do it from whichever thread happened to be asking,
        # racing BTC's own snapshot. Using what BTC reported is both
        # cheaper and the number BTC actually stands behind.
        btc_flow = btc_book = None
        if btc_state is not None and btc_state is not self:
            btc_frame = btc_state.cached_snapshot()
            if btc_frame:
                layers_block = (btc_frame.get("pressure") or {}).get("layers") or {}
                flow_block = layers_block.get("flow") or {}
                if flow_block.get("score") is not None:
                    btc_flow = float(flow_block["score"])
            if btc_state.book.synced:
                btc_book = btc_state.book.pressure_component()

        return {
            "flow": score_flow(self.flow, self.cvd, self.normalizer),
            "book": score_book(self.book, self.microprice, self.normalizer),
            "structure": score_structure(smc_state, elliott_component, price),
            "derivatives": score_derivatives(self.open_interest, self.liquidations,
                                             self.ticker, self.normalizer),
            "btc_lead": score_btc_lead(lead, btc_flow, btc_book,
                                       is_btc=self.symbol == BTC_SYMBOL),
        }

    def live_candle(self, interval_seconds: int,
                    now_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """The forming bar for any timeframe, from Bybit's OWN klines.

        The browser used to build this itself out of a price polled every
        500ms and its own clock, which lost every high and low between two
        polls and reported a volume of zero. The exchange sends the real
        thing on `kline.1` - open, high, low, close, volume and `confirm` -
        so the bar is aggregated from those one-minute klines rather than
        invented.

        Aggregating from the ONE-MINUTE stream rather than subscribing to
        every chart timeframe is deliberate: five intervals across seven
        symbols was twenty-eight topics of bandwidth for data nothing read,
        and it is exactly what overloaded the socket. One minute is the
        finest bar the chart offers, so every coarser one is an exact sum
        of them.

        `closed` is false by construction - this is the bar still forming.
        `confirm_source` says how much of it Bybit has confirmed, so a
        caller can tell a complete aggregate from one whose last minute is
        still moving."""
        seconds = max(60, int(interval_seconds))
        engine = self.smc.get(STRUCTURE_INTERVAL)
        candles = list(engine.candles) if engine is not None else []
        if not candles:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        bar_start_ms = (now_ms // (seconds * 1000)) * (seconds * 1000)
        inside = [c for c in candles if c.start_ms >= bar_start_ms]
        if not inside:
            return None
        inside.sort(key=lambda c: c.start_ms)
        confirmed = sum(1 for c in inside if c.closed)
        return {
            "time": bar_start_ms // 1000,
            "open": inside[0].open,
            "high": max(c.high for c in inside),
            "low": min(c.low for c in inside),
            "close": inside[-1].close,
            "volume": sum(c.volume for c in inside),
            "closed": False,
            "interval_seconds": seconds,
            # How the bar was built, so nothing downstream has to guess.
            "source": "bybit_kline_1m",
            "minutes_used": len(inside),
            "minutes_confirmed": confirmed,
            "last_minute_confirmed": inside[-1].closed,
            "as_of_ms": max(c.start_ms for c in inside),
        }

    def cached_snapshot(self) -> Optional[Dict[str, Any]]:
        """The last computed frame, or None. Never recomputes - callers
        that need one computed call `snapshot()`."""
        return self._snapshot

    def snapshot(self, force: bool = False, now: Optional[float] = None,
                 btc_state: Optional["SymbolState"] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        # Checked before taking the lock as well as after: a cache hit is
        # the common case and should not queue behind a recompute.
        if not force and self._snapshot is not None and \
                (now - self._snapshot_at) < self.config.recompute_interval_seconds:
            return self._snapshot
        with self._snapshot_lock:
            if not force and self._snapshot is not None and \
                    (now - self._snapshot_at) < self.config.recompute_interval_seconds:
                return self._snapshot
            return self._compute_snapshot(force, now, btc_state)

    def _compute_snapshot(self, force: bool, now: float,
                          btc_state: Optional["SymbolState"]) -> Dict[str, Any]:
        now_ms = int(now * 1000)
        book_metrics = self.book.metrics()
        layers = self.layer_scores(btc_state)
        pressure = pressure_score(layers, self.config.layer_weights)
        flow_component = layers["flow"].score

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
            cvd_component=layers["flow"].detail.get("cvd_slope", 0.0),
            flow_component=flow_component,
            velocity_zscore=self.flow.velocity_zscore(),
            velocity_acceleration=self.flow.acceleration(),
            btc_lead_score=float(lead.get("btc_lead_score") or 0.0),
            candles=candles,
        )
        long_break = self.prebreak.evaluate(LONG, prebreak_inputs)
        short_break = self.prebreak.evaluate(SHORT, prebreak_inputs)

        # Open a calibration observation for anything worth tracking, and
        # ask whether this score has earned the word "probability" yet.
        # Opening happens BEFORE the outcome exists - that is the whole
        # design; see calibration.py.
        for result in (long_break, short_break):
            if result.level:
                self.calibrator.observe(result.direction, result.probability,
                                        result.level, price, now_ms)
        long_label = label_for(long_break.probability,
                               self.calibrator.probability(long_break.probability,
                                                           15_000, LONG))
        short_label = label_for(short_break.probability,
                                self.calibrator.probability(short_break.probability,
                                                            15_000, SHORT))

        verdict = assess(self.health, self.thresholds, now_ms=now_ms, started=True)
        signal = self.signals.update(SignalInputs(
            long_pressure=pressure.long_pressure,
            short_pressure=pressure.short_pressure,
            conflict=min(pressure.long_pressure, pressure.short_pressure),
            conflict_level=(pressure.conflict.level if pressure.conflict
                            else "CONFLICT_LOW"),
            confidence=pressure.confidence,
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
            "walls": self.book.wall_summary(now_ms) if self.book.synced else {},
            "microprice": self.microprice.as_dict(),
            "trade_flow": self.flow.as_dict(),
            "cvd": self.cvd.as_dict(),
            "liquidations": self.liquidations.as_dict(),
            "open_interest": self.open_interest.as_dict(),
            "btc_lead": lead,
            "smc": smc_state,
            "elliott": self.elliott.as_dict(),
            "pressure": pressure.as_dict(),
            "layers": {name: layer.as_dict() for name, layer in layers.items()},
            "prebreak": {
                "long": {**long_break.as_dict(), "calibration": long_label},
                "short": {**short_break.as_dict(), "calibration": short_label},
                # What the level tracker had to work with, and why it
                # found nothing when it found nothing. "No resistance
                # identified below visible swings" was not a diagnosis.
                "levels": self.prebreak.levels.as_dict(price),
            },
            "calibration": self.calibrator.summary(),
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
