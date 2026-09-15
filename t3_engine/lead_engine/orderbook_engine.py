"""A local L2 book rebuilt from Bybit's snapshot + delta stream.

Bybit publishes `orderbook.50.SYMBOL` as one snapshot followed by deltas,
each carrying an update id `u` that increments by exactly one. That last
detail is the whole reason this module exists rather than polling REST:
the sequence is what lets the book know it is CORRECT. A delta applied
over a gap produces a book that looks fine, prices that look fine, and an
imbalance that is quietly wrong - the worst failure mode available here,
because nothing downstream can detect it. So a gap sets `synced = False`
and every metric stops being offered until a fresh snapshot arrives.

What this computes beyond the raw book:

  - OBI at 1, 5, 10, 25 and 50 levels, plus a distance-weighted variant.
    Depth far from the touch is real but it is not what the next hundred
    milliseconds trade against, and an unweighted 50-level imbalance is
    dominated by size nobody will reach.
  - Pulling and replenishment, per side, as rates. This is the part that
    needs the trade stream: size leaving the bid because it was BOUGHT is
    not the same event as size leaving because it was CANCELLED, and the
    second one is the interesting one. `note_trade` feeds executions in so
    that `apply` can subtract them from the observed decrease and call
    only the remainder a pull.
  - Walls, their persistence, and their cancellation - again separating
    "eaten" from "withdrawn".
  - Absorption: size being replaced as fast as it is hit.

Everything is bounded and per-symbol. Nothing here is written to any
structure belonging to the rest of the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from t3_engine.lead_engine.config import Thresholds
from t3_engine.lead_engine.rolling import TimeSeries, clamp, median

# The level counts the specification asks for.
OBI_DEPTHS: Tuple[int, ...] = (1, 5, 10, 25, 50)

# How far back pulling / replenishment / absorption are measured.
FLOW_WINDOW_MS = 5_000

# A level is "stacked" with its neighbours at this multiple of the median.
STACK_MULTIPLE = 2.0


@dataclass
class LevelFlow:
    """Size added to and taken off one side between two book updates."""

    added: float = 0.0
    removed: float = 0.0
    executed: float = 0.0

    @property
    def cancelled(self) -> float:
        """Removal that trading does not account for - a pull.

        Floored at zero: a burst can report more execution than this
        module saw removed (the level was refilled between updates), and a
        negative 'cancellation' is not a thing."""
        return max(0.0, self.removed - self.executed)


@dataclass
class Wall:
    side: str
    price: float
    size: float
    first_seen_ms: int
    last_seen_ms: int

    @property
    def persistence_ms(self) -> int:
        return max(0, self.last_seen_ms - self.first_seen_ms)


@dataclass
class OrderBookMetrics:
    symbol: str
    synced: bool
    updated_at_ms: int
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    spread_bps: Optional[float] = None
    midpoint: Optional[float] = None
    microprice: Optional[float] = None
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    imbalance: float = 0.0
    obi: Dict[str, float] = field(default_factory=dict)
    weighted_obi: float = 0.0
    bid_pulling: float = 0.0
    ask_pulling: float = 0.0
    bid_replenishment: float = 0.0
    ask_replenishment: float = 0.0
    bid_absorption: float = 0.0
    ask_absorption: float = 0.0
    stacked_bid_levels: int = 0
    stacked_ask_levels: int = 0
    bid_walls: int = 0
    ask_walls: int = 0
    max_wall_persistence_ms: int = 0
    wall_cancellations: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol, "synced": self.synced, "updated_at_ms": self.updated_at_ms,
            "best_bid": self.best_bid, "best_ask": self.best_ask, "spread": self.spread,
            "spread_bps": self.spread_bps, "midpoint": self.midpoint,
            "microprice": self.microprice, "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth, "imbalance": self.imbalance, "obi": dict(self.obi),
            "weighted_obi": self.weighted_obi, "bid_pulling": self.bid_pulling,
            "ask_pulling": self.ask_pulling, "bid_replenishment": self.bid_replenishment,
            "ask_replenishment": self.ask_replenishment, "bid_absorption": self.bid_absorption,
            "ask_absorption": self.ask_absorption, "stacked_bid_levels": self.stacked_bid_levels,
            "stacked_ask_levels": self.stacked_ask_levels, "bid_walls": self.bid_walls,
            "ask_walls": self.ask_walls,
            "max_wall_persistence_ms": self.max_wall_persistence_ms,
            "wall_cancellations": self.wall_cancellations,
        }


class OrderBook:
    """One instrument's book. Not thread-safe by itself; the engine owns
    one per symbol and touches it only from the stream thread."""

    def __init__(self, symbol: str, depth: int = 50,
                 thresholds: Optional[Thresholds] = None) -> None:
        self.symbol = symbol.upper()
        self.depth = depth
        self.thresholds = thresholds or Thresholds()
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.synced = False
        self.last_update_id: Optional[int] = None
        self.updated_at_ms = 0
        self.snapshots = 0
        self.deltas_applied = 0
        self.gaps = 0
        self.crossings = 0
        self._pending_exec: Dict[str, float] = {"bid": 0.0, "ask": 0.0}
        self._flows: TimeSeries = TimeSeries(horizon_ms=FLOW_WINDOW_MS * 4)
        self._walls: Dict[Tuple[str, float], Wall] = {}
        self._wall_cancels: TimeSeries = TimeSeries(horizon_ms=60_000)

    # ---- ingest ----

    def apply(self, message: Dict) -> bool:
        """Apply one orderbook message. False when it was not usable.

        A snapshot always applies and always re-syncs. A delta applies
        only when it continues the sequence; anything else is a gap, and a
        gap desyncs rather than being papered over."""
        data = message.get("data") or {}
        kind = str(message.get("type") or "").lower()
        stamp = int(message.get("ts") or message.get("cts") or 0)
        try:
            update_id = int(data.get("u"))
        except (TypeError, ValueError):
            update_id = None

        if kind == "snapshot":
            self.bids.clear()
            self.asks.clear()
            self._merge(data)
            self.last_update_id = update_id
            self.snapshots += 1
            if self._crossed():
                self.crossings += 1
                self.synced = False
                return False
            self.synced = True
            self.updated_at_ms = stamp or self.updated_at_ms
            self._pending_exec = {"bid": 0.0, "ask": 0.0}
            return True

        if kind != "delta":
            return False
        if not self.synced:
            return False                # nothing to apply a delta to yet
        if update_id is not None and self.last_update_id is not None:
            if update_id <= self.last_update_id:
                return False            # a repeat; harmless, and not an error
            if update_id != self.last_update_id + 1:
                # The book can no longer be trusted. Said out loud rather
                # than repaired: a silently wrong book is the failure this
                # whole sequence check exists to prevent.
                self.gaps += 1
                self.synced = False
                return False

        before_bids = dict(self.bids)
        before_asks = dict(self.asks)
        self._merge(data)
        self.last_update_id = update_id if update_id is not None else self.last_update_id
        self.deltas_applied += 1
        self.updated_at_ms = stamp or self.updated_at_ms
        if self._crossed():
            # A best bid at or above the best ask cannot exist on an
            # exchange, so a local book showing one is wrong - a missed
            # removal, a mangled level, a delta applied to the wrong side.
            # Found by a replay, where a fixture that never removed stale
            # bids produced a book quoting 5.90 bid / 5.65 ask and a
            # perfectly well-formed imbalance computed from it. The
            # sequence check cannot catch this (the ids were contiguous),
            # so it is checked directly, and the response is the same:
            # desync and wait for a snapshot rather than serve numbers
            # derived from a book that cannot be real.
            self.crossings += 1
            self.synced = False
            return False
        self._record_flow(before_bids, before_asks, stamp)
        self._track_walls(stamp)
        return True

    def _crossed(self) -> bool:
        bid, ask = self.best_bid(), self.best_ask()
        return bid is not None and ask is not None and bid >= ask

    def _merge(self, data: Dict) -> None:
        for raw, book in ((data.get("b") or [], self.bids), (data.get("a") or [], self.asks)):
            for entry in raw:
                try:
                    price = float(entry[0])
                    size = float(entry[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if size <= 0:
                    book.pop(price, None)
                else:
                    book[price] = size

    def note_trade(self, taker_side: str, quantity: float) -> None:
        """Tell the book that liquidity was EXECUTED, not withdrawn.

        A taker Buy lifts offers, so it consumes the ASK side; a taker
        Sell hits bids. Without this the engine would read every filled
        order as a cancelled one and report constant 'pulling' in exactly
        the conditions - heavy trading - where pulling matters most."""
        side = "ask" if str(taker_side).lower().startswith("b") else "bid"
        self._pending_exec[side] += max(0.0, float(quantity))

    # ---- flow bookkeeping ----

    def _record_flow(self, before_bids: Dict[float, float],
                     before_asks: Dict[float, float], stamp: int) -> None:
        bid_flow = self._diff(before_bids, self.bids)
        ask_flow = self._diff(before_asks, self.asks)
        bid_flow.executed = self._pending_exec["bid"]
        ask_flow.executed = self._pending_exec["ask"]
        self._pending_exec = {"bid": 0.0, "ask": 0.0}
        self._flows.add(stamp or self.updated_at_ms, (bid_flow, ask_flow))

    @staticmethod
    def _diff(before: Dict[float, float], after: Dict[float, float]) -> LevelFlow:
        flow = LevelFlow()
        for price, size in after.items():
            previous = before.get(price, 0.0)
            if size > previous:
                flow.added += size - previous
        for price, size in before.items():
            current = after.get(price, 0.0)
            if current < size:
                flow.removed += size - current
        return flow

    def _flow_totals(self, window_ms: int = FLOW_WINDOW_MS) -> Tuple[LevelFlow, LevelFlow]:
        bids, asks = LevelFlow(), LevelFlow()
        for entry in self._flows.window(window_ms):
            bid_flow, ask_flow = entry            # type: ignore[misc]
            bids.added += bid_flow.added
            bids.removed += bid_flow.removed
            bids.executed += bid_flow.executed
            asks.added += ask_flow.added
            asks.removed += ask_flow.removed
            asks.executed += ask_flow.executed
        return bids, asks

    # ---- walls ----

    def _track_walls(self, stamp: int) -> None:
        stamp = stamp or self.updated_at_ms
        threshold = self.thresholds.wall_multiple * self._median_level_size()
        live: Dict[Tuple[str, float], Wall] = {}
        if threshold > 0:
            for side, book in (("bid", self.bids), ("ask", self.asks)):
                for price, size in self._top(book, side, self.depth):
                    if size < threshold:
                        continue
                    key = (side, price)
                    existing = self._walls.get(key)
                    if existing is None:
                        live[key] = Wall(side, price, size, stamp, stamp)
                    else:
                        existing.size = size
                        existing.last_seen_ms = stamp
                        live[key] = existing
        for key, wall in self._walls.items():
            if key in live:
                continue
            # Gone. Traded through, or pulled? If the book still shows a
            # touch on the far side of that price, it was consumed; if
            # price never reached it, someone withdrew it.
            if not self._price_reached(wall):
                self._wall_cancels.add(stamp, wall)
        self._walls = live

    def _price_reached(self, wall: Wall) -> bool:
        if wall.side == "bid":
            best = self.best_bid()
            return best is not None and best <= wall.price
        best = self.best_ask()
        return best is not None and best >= wall.price

    # ---- views ----

    @staticmethod
    def _top(book: Dict[float, float], side: str, count: int) -> List[Tuple[float, float]]:
        ordered = sorted(book.items(), key=lambda kv: kv[0], reverse=(side == "bid"))
        return ordered[:count]

    def top_bids(self, count: Optional[int] = None) -> List[Tuple[float, float]]:
        return self._top(self.bids, "bid", count or self.depth)

    def top_asks(self, count: Optional[int] = None) -> List[Tuple[float, float]]:
        return self._top(self.asks, "ask", count or self.depth)

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def _median_level_size(self) -> float:
        sizes = [size for _, size in self.top_bids()] + [size for _, size in self.top_asks()]
        return median(sizes)

    def _stacked(self, levels: List[Tuple[float, float]]) -> int:
        """How many levels IN A ROW from the touch are well above typical.

        Stacked liquidity is a run, not a count of big levels scattered
        through the book - one large order five levels down says nothing
        about the next tick."""
        reference = self._median_level_size()
        if reference <= 0:
            return 0
        run = 0
        for _, size in levels:
            if size >= STACK_MULTIPLE * reference:
                run += 1
            else:
                break
        return run

    # ---- metrics ----

    def obi(self, depth: int) -> float:
        bids = sum(size for _, size in self.top_bids(depth))
        asks = sum(size for _, size in self.top_asks(depth))
        total = bids + asks
        return (bids - asks) / total if total > 0 else 0.0

    def weighted_obi(self) -> float:
        """OBI with each level discounted by its distance from the touch.

        Weight 1/(1+distance/spread-ish): the first level counts fully, a
        level a hundred ticks away barely counts. Uses the midpoint as the
        reference so the discount is symmetric between the two sides."""
        mid = self.midpoint()
        if mid is None or mid <= 0:
            return 0.0
        bid_weighted = sum(size / (1.0 + abs(mid - price) / mid * 1000.0)
                           for price, size in self.top_bids())
        ask_weighted = sum(size / (1.0 + abs(price - mid) / mid * 1000.0)
                           for price, size in self.top_asks())
        total = bid_weighted + ask_weighted
        return (bid_weighted - ask_weighted) / total if total > 0 else 0.0

    def midpoint(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    def microprice(self) -> Optional[float]:
        """The size-weighted touch: the price the book is leaning toward.

        Weighted by the OPPOSITE side's size, which is the standard form
        and the one that behaves correctly - a huge bid and a thin offer
        means the next trade is likelier to print at the offer, so the
        microprice sits near the ask."""
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        bid_size = self.bids.get(bid, 0.0)
        ask_size = self.asks.get(ask, 0.0)
        total = bid_size + ask_size
        if total <= 0:
            return (bid + ask) / 2.0
        return (bid * ask_size + ask * bid_size) / total

    def metrics(self, window_ms: int = FLOW_WINDOW_MS) -> OrderBookMetrics:
        """Everything, once. Returns an unsynced, empty metric set rather
        than plausible numbers when the book is not trustworthy."""
        out = OrderBookMetrics(symbol=self.symbol, synced=self.synced,
                               updated_at_ms=self.updated_at_ms)
        if not self.synced or not self.bids or not self.asks:
            out.synced = False
            return out

        bid, ask = self.best_bid(), self.best_ask()
        mid = self.midpoint()
        out.best_bid, out.best_ask, out.midpoint = bid, ask, mid
        out.microprice = self.microprice()
        if bid is not None and ask is not None:
            out.spread = ask - bid
            out.spread_bps = (out.spread / mid * 10_000.0) if mid else None
        out.bid_depth = sum(size for _, size in self.top_bids())
        out.ask_depth = sum(size for _, size in self.top_asks())
        total_depth = out.bid_depth + out.ask_depth
        out.imbalance = ((out.bid_depth - out.ask_depth) / total_depth) if total_depth else 0.0
        out.obi = {f"obi{depth}": round(self.obi(depth), 6) for depth in OBI_DEPTHS}
        out.weighted_obi = round(self.weighted_obi(), 6)

        bid_flow, ask_flow = self._flow_totals(window_ms)
        seconds = max(0.001, window_ms / 1000.0)
        out.bid_pulling = bid_flow.cancelled / seconds
        out.ask_pulling = ask_flow.cancelled / seconds
        out.bid_replenishment = bid_flow.added / seconds
        out.ask_replenishment = ask_flow.added / seconds
        # Absorption: refilled as fast as it was hit. Only meaningful when
        # something actually traded into that side.
        out.bid_absorption = (bid_flow.added / bid_flow.executed) if bid_flow.executed > 0 else 0.0
        out.ask_absorption = (ask_flow.added / ask_flow.executed) if ask_flow.executed > 0 else 0.0

        out.stacked_bid_levels = self._stacked(self.top_bids())
        out.stacked_ask_levels = self._stacked(self.top_asks())
        out.bid_walls = sum(1 for (side, _), _ in self._walls.items() if side == "bid")
        out.ask_walls = sum(1 for (side, _), _ in self._walls.items() if side == "ask")
        out.max_wall_persistence_ms = max((w.persistence_ms for w in self._walls.values()),
                                          default=0)
        out.wall_cancellations = len(self._wall_cancels.window(60_000))
        return out

    def pressure_component(self) -> float:
        """One number on -1..+1 for the pressure score: how the resting
        book leans. Blends the weighted OBI with the shallow one so a
        thin, aggressive touch cannot be hidden by depth further out."""
        if not self.synced:
            return 0.0
        return clamp(0.6 * self.weighted_obi() + 0.4 * self.obi(5))
