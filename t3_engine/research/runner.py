"""The decision loop. ONE of them, shared by replay and live PAPER.

This is the module that makes a forward test a test of the thing that
was backtested. The strategy object, the feature engine, the execution
model and the portfolio are identical in both; what differs is only

  * where events come from (a recorded segment, or a live socket),
  * which clock stamps them, and
  * which execution adapter the orders go to.

If any decision logic lived in the backtest harness instead of here, the
PAPER run would be testing a different program.

EVENT ORDER IS RECEIVE ORDER. Not exchange order: the process cannot act
on what it has not been told, and a capture from one connection learned
its events in receive order by definition. Fills, separately, happen
against EXCHANGE time, because that is when the liquidity was really
there. Keeping those two apart is most of the honesty in this file.

DATA QUALITY GATES ENTRIES, AND ONLY ENTRIES. A stale book, a gap in the
tape, a resync or a disconnect blocks NEW positions. It does not block
exits: a position that exists has to be manageable, and refusing to
close it because the feed hiccuped is how a bounded loss becomes an
unbounded one. Anything opened or closed while quality was degraded is
flagged in the journal so it can be excluded from a result later,
instead of quietly counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from t3_engine.research.book import BUY, SELL, BookReconstructor, BookState, Trade
from t3_engine.research.execution import (LIMIT, MARKET, ExecutionSimulator,
                                          InstrumentSpec, LatencyModel, Order,
                                          QueueModel)
from t3_engine.research.features import FeatureEngine, MarketView
from t3_engine.research.portfolio import (EXIT_DATA_GAP, EXIT_STOP, EXIT_TARGET,
                                          EXIT_TIME, LONG, QUALITY_DATA_GAP,
                                          QUALITY_OK, QUALITY_STALE, SHORT,
                                          Portfolio, RiskLimits)
from t3_engine.research.strategies import (INTENT_CANCEL, INTENT_ENTER,
                                           INTENT_EXIT, INTENT_QUOTE, Intent,
                                           Strategy)

# A book older than this is not a book, it is a memory.
STALE_BOOK_MS = 2_000
# A silence longer than this in the event stream is a gap in the data,
# not a quiet market, and nothing may be opened across it.
GAP_MS = 10_000
# After a gap, this much fresh data is required before entries resume.
REWARM_MS = 5_000


@dataclass
class RunnerConfig:
    symbol: str
    spec: InstrumentSpec
    latency: LatencyModel = field(default_factory=LatencyModel)
    queue: QueueModel = field(default_factory=QueueModel.conservative)
    limits: RiskLimits = field(default_factory=RiskLimits)
    window_ms: int = 3_000
    vol_window_ms: int = 30_000
    stale_book_ms: int = STALE_BOOK_MS
    gap_ms: int = GAP_MS
    rewarm_ms: int = REWARM_MS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_size": self.spec.tick_size, "qty_step": self.spec.qty_step,
            "min_notional": self.spec.min_notional,
            "latency": {"send_ms": self.latency.send_ms, "ack_ms": self.latency.ack_ms,
                        "cancel_ms": self.latency.cancel_ms},
            "queue": {"queue_factor": self.queue.queue_factor,
                      "cancel_helps": self.queue.cancel_helps},
            "limits": self.limits.as_dict(),
            "window_ms": self.window_ms, "vol_window_ms": self.vol_window_ms,
            "stale_book_ms": self.stale_book_ms, "gap_ms": self.gap_ms,
        }


class StrategyRunner:
    def __init__(self, strategy: Strategy, config: RunnerConfig, *,
                 portfolio: Optional[Portfolio] = None) -> None:
        self.strategy = strategy
        self.config = config
        self.symbol = config.symbol.upper()
        self.features = FeatureEngine(self.symbol, window_ms=config.window_ms,
                                      vol_window_ms=config.vol_window_ms)
        self.sim = ExecutionSimulator(config.spec, latency=config.latency,
                                      queue=config.queue,
                                      fees=strategy.costs.fees)
        merged = {"strategy": strategy.config().as_dict(),
                  "runner": config.as_dict()}
        self.portfolio = portfolio or Portfolio(
            strategy_version=f"{strategy.name}@{strategy.version}",
            config=merged, limits=config.limits, fees=strategy.costs.fees)

        self.book: Optional[BookState] = None
        self.quality = QUALITY_OK
        self.quality_reason = ""
        self._last_event_ms = 0
        self._good_since_ms = 0
        self._entry_order: Optional[Order] = None
        self._exit_order: Optional[Order] = None
        self._quotes: Dict[str, Order] = {}
        self._pending_intent: Optional[Intent] = None
        self._position_entry_ms = 0
        self._signals = 0
        self._episode = 0
        self._last_signal_ms = 0
        self.gaps = 0
        self.stale_events = 0

    # ---- the loop --------------------------------------------------------

    def on_book(self, book: BookState) -> None:
        self._tick(book.recv_ms or book.exchange_ms)
        self.book = book
        self.sim.on_book(book)
        self.features.on_book(book)
        self._after_market_event(book.recv_ms or book.exchange_ms,
                                 book.exchange_ms)

    def on_trade(self, trade: Trade) -> None:
        self._tick(trade.recv_ms or trade.exchange_ms)
        self.sim.on_trade(trade)
        self.features.on_trade(trade)
        self._after_market_event(trade.recv_ms or trade.exchange_ms,
                                 trade.exchange_ms)

    def on_liquidation(self, at_ms: int, notional: float, side: str) -> None:
        self.features.on_liquidation(at_ms, notional, side)

    def _tick(self, recv_ms: int) -> None:
        """Gap detection, before anything reads the feed."""
        if self._last_event_ms and recv_ms - self._last_event_ms > self.config.gap_ms:
            self.gaps += 1
            self.quality = QUALITY_DATA_GAP
            self.quality_reason = (f"{(recv_ms - self._last_event_ms) / 1000.0:.1f}s "
                                   f"with no data")
            self._good_since_ms = recv_ms
        self._last_event_ms = recv_ms

    def _assess(self, recv_ms: int) -> None:
        book = self.book
        if book is None or not book.valid:
            self.quality = QUALITY_STALE
            self.quality_reason = "no usable book"
            self._good_since_ms = recv_ms
            return
        age = recv_ms - (book.recv_ms or recv_ms)
        if age > self.config.stale_book_ms:
            self.stale_events += 1
            self.quality = QUALITY_STALE
            self.quality_reason = f"book {age}ms old"
            self._good_since_ms = recv_ms
            return
        if self.quality != QUALITY_OK:
            # Recovering: require a clean stretch before entries resume,
            # so a single good frame after a two-minute hole does not
            # look like a healthy feed.
            if recv_ms - self._good_since_ms < self.config.rewarm_ms:
                return
        self.quality = QUALITY_OK
        self.quality_reason = ""

    @property
    def may_enter(self) -> bool:
        return self.quality == QUALITY_OK

    def _after_market_event(self, recv_ms: int, exchange_ms: int) -> None:
        self._assess(recv_ms)
        self._settle_orders()

        flat = self.symbol not in self.portfolio.positions
        position = self.portfolio.positions.get(self.symbol)
        position_bps = 0.0
        held_ms = 0
        if position is not None and self.book is not None and self.book.valid:
            mark = self.book.mid
            position_bps = 10_000.0 * position.unrealised(mark) / position.notional
            held_ms = max(0, exchange_ms - position.entry_at_ms)

        view = self.features.view(recv_ms, exchange_ms)
        intents = self.strategy.on_view(
            view, flat=flat, position_side="" if flat else position.direction,
            position_bps=position_bps, held_ms=held_ms)
        for intent in intents:
            self._act(intent, view, flat)

    # ---- intents ---------------------------------------------------------

    def _act(self, intent: Intent, view: MarketView, flat: bool) -> None:
        if intent.kind == INTENT_ENTER and flat:
            self._enter(intent, view)
        elif intent.kind == INTENT_EXIT and not flat:
            self._exit(intent, view)
        elif intent.kind == INTENT_QUOTE:
            self._quote(intent, view)
        elif intent.kind == INTENT_CANCEL:
            for order in list(self.sim.open_orders):
                self.sim.cancel(order.order_id, view.decided_at_ms)

    def _enter(self, intent: Intent, view: MarketView) -> None:
        if not self.may_enter:
            self.portfolio.decline(f"data quality: {self.quality_reason}")
            return
        if self._entry_order is not None and not self._entry_order.terminal:
            return
        notional = intent.qty * (view.mid or 0.0)
        allowed, why = self.portfolio.allows(self.symbol, notional,
                                             view.decided_at_ms)
        if not allowed:
            self.portfolio.decline(f"risk limit: {why}")
            return

        self._signals += 1
        # An episode groups signals that are really one event. Repeated
        # snapshots of the same setup are NOT independent observations
        # and must not be counted as separate evidence.
        if view.decided_at_ms - self._last_signal_ms > 10 * 60 * 1000:
            self._episode += 1
        self._last_signal_ms = view.decided_at_ms

        self._entry_order = self.sim.submit(
            intent.side, intent.qty, kind=intent.order_kind, price=intent.price,
            post_only=intent.post_only, decided_at_ms=view.decided_at_ms,
            ttl_ms=intent.ttl_ms, tag=intent.tag)
        self._pending_intent = intent

    def _exit(self, intent: Intent, view: MarketView) -> None:
        if self._exit_order is not None and not self._exit_order.terminal:
            return
        position = self.portfolio.positions.get(self.symbol)
        if position is None:
            return
        side = SELL if position.direction == LONG else BUY
        self._exit_order = self.sim.submit(
            side, position.qty, kind=intent.order_kind, price=intent.price,
            post_only=intent.post_only, decided_at_ms=view.decided_at_ms,
            ttl_ms=intent.ttl_ms, tag=intent.tag or "exit", reduce_only=True)
        self._exit_reason = intent.reason or "SIGNAL_EXIT"

    def _quote(self, intent: Intent, view: MarketView) -> None:
        if not self.may_enter:
            self.portfolio.decline(f"data quality: {self.quality_reason}")
            return
        existing = self._quotes.get(intent.tag)
        if existing is not None and not existing.terminal:
            return
        notional = intent.qty * (view.mid or 0.0)
        allowed, why = self.portfolio.allows(self.symbol, notional,
                                             view.decided_at_ms)
        if not allowed and self.symbol not in self.portfolio.positions:
            self.portfolio.decline(f"risk limit: {why}")
            return
        self._quotes[intent.tag] = self.sim.submit(
            intent.side, intent.qty, kind=LIMIT, price=intent.price,
            post_only=True, decided_at_ms=view.decided_at_ms,
            ttl_ms=intent.ttl_ms, tag=intent.tag)

    # ---- turning fills into positions ------------------------------------

    def _settle_orders(self) -> None:
        entry = self._entry_order
        if entry is not None and entry.fills and self.symbol not in self.portfolio.positions:
            direction = LONG if entry.side == BUY else SHORT
            self.portfolio.open_position(
                symbol=self.symbol, direction=direction, fills=entry.fills,
                signal_id=f"{self.symbol}:{entry.decided_at_ms}",
                episode_id=f"{self.symbol}:{self._episode}",
                quality=self.quality)
            self._position_entry_ms = entry.fills[-1].at_ms
            self._entry_order = None

        # A quote that filled IS a position. This is the case the naive
        # market-making backtest forgets: one side fills, the other does
        # not, and the inventory is now a directional bet.
        for tag, order in list(self._quotes.items()):
            if order.fills and self.symbol not in self.portfolio.positions:
                direction = LONG if order.side == BUY else SHORT
                self.portfolio.open_position(
                    symbol=self.symbol, direction=direction, fills=order.fills,
                    signal_id=f"{self.symbol}:quote:{order.decided_at_ms}",
                    episode_id=f"{self.symbol}:{self._episode}",
                    quality=self.quality)
                self._position_entry_ms = order.fills[-1].at_ms
                # The other side is pulled: we are no longer flat, so the
                # quote that is left would double the position.
                for other_tag, other in list(self._quotes.items()):
                    if other_tag != tag and not other.terminal:
                        self.sim.cancel(other.order_id, order.fills[-1].at_ms)
                self._quotes.pop(tag, None)

        exit_order = self._exit_order
        if exit_order is not None and exit_order.fills:
            self.portfolio.close_position(
                self.symbol, fills=exit_order.fills,
                reason=getattr(self, "_exit_reason", "SIGNAL_EXIT"),
                quality=self.quality if self.quality == QUALITY_OK else self.quality)
            self._exit_order = None

    # ---- the end ---------------------------------------------------------

    def finalise(self, at_ms: Optional[int] = None) -> None:
        """Close the books honestly: value whatever is still open by
        selling it into the book that exists."""
        stamp = at_ms or self._last_event_ms
        books = {self.symbol: self.book} if self.book is not None else {}
        self.portfolio.finalise(books, stamp)

    def report(self) -> Dict[str, Any]:
        out = self.portfolio.report()
        out["execution"] = self.sim.stats()
        out["signals"] = self._signals
        out["episodes_seen"] = self._episode
        out["data_gaps"] = self.gaps
        out["stale_events"] = self.stale_events
        out["strategy_declines"] = dict(self.strategy.declines)
        return out
