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


# Wall classification. A large resting order is not a wall the moment it
# appears - most of them are gone in seconds, and treating a fresh one as
# support is how an engine gets walked into a spoof. These thresholds are
# what separate "large" from "meaningful".
TRANSIENT_WALL_MS = 3_000
PERSISTENT_WALL_MS = 20_000

# A wall counts as replenishing once it has been cut and rebuilt this
# often - the shape of a real defender, as opposed to one order sitting
# there untouched.
REPLENISH_EVENTS = 2

# ...and as absorbed once this share of its own size has traded at its
# price while it was still standing.
ABSORBED_SHARE = 0.75

TRANSIENT_WALL = "TRANSIENT_WALL"
PERSISTENT_WALL = "PERSISTENT_WALL"
REPLENISHING_WALL = "REPLENISHING_WALL"
POSSIBLE_SPOOF = "POSSIBLE_SPOOF"
ABSORBED_WALL = "ABSORBED_WALL"


@dataclass
class Wall:
    """One large resting order, and everything needed to judge it.

    The first build kept only first/last seen. That cannot tell a wall
    that has stood for ten minutes from one that appeared a second ago,
    cannot tell a defender being repeatedly cut and rebuilt from an
    untouched block, and cannot tell an order that was traded through
    from one that was pulled the moment price approached. All three
    distinctions change what the wall means, so all three are tracked."""

    side: str
    price: float
    size: float
    first_seen_ms: int
    last_seen_ms: int
    initial_size: float = 0.0
    max_size: float = 0.0
    min_size: float = 0.0
    updates: int = 0
    replenishments: int = 0
    reductions: int = 0
    executed: float = 0.0
    reached: bool = False

    def __post_init__(self) -> None:
        if self.initial_size <= 0:
            self.initial_size = self.size
        if self.max_size <= 0:
            self.max_size = self.size
        if self.min_size <= 0:
            self.min_size = self.size

    @property
    def persistence_ms(self) -> int:
        return max(0, self.last_seen_ms - self.first_seen_ms)

    @property
    def absorbed_share(self) -> float:
        return self.executed / self.initial_size if self.initial_size > 0 else 0.0

    def observe(self, size: float, timestamp_ms: int) -> None:
        previous = self.size
        self.size = size
        self.last_seen_ms = timestamp_ms
        self.updates += 1
        self.max_size = max(self.max_size, size)
        self.min_size = min(self.min_size, size)
        if size < previous * 0.8:
            self.reductions += 1
        elif size > previous * 1.2 and self.reductions > 0:
            # Cut and rebuilt. That is a defender, not a block.
            self.replenishments += 1

    def classify(self, now_ms: int) -> str:
        """What this wall is, right now.

        Order matters: absorption and replenishment are statements about
        what HAPPENED to the wall and outrank the age buckets, which are
        statements about how long it has merely existed."""
        if self.absorbed_share >= ABSORBED_SHARE:
            return ABSORBED_WALL
        if self.replenishments >= REPLENISH_EVENTS:
            return REPLENISHING_WALL
        age = max(0, now_ms - self.first_seen_ms)
        if age < TRANSIENT_WALL_MS:
            return TRANSIENT_WALL
        if age >= PERSISTENT_WALL_MS:
            return PERSISTENT_WALL
        return TRANSIENT_WALL

    def weight(self, now_ms: int) -> float:
        """How much this wall should count, 0..1.

        Only persistent and replenishing walls carry real weight. A
        transient one counts for almost nothing, an absorbed one counts
        against the side it was defending, and a wall that vanished
        without price ever reaching it is scored as a spoof by the book,
        not here."""
        kind = self.classify(now_ms)
        if kind == PERSISTENT_WALL:
            return 1.0
        if kind == REPLENISHING_WALL:
            return 0.9
        if kind == ABSORBED_WALL:
            return 0.2
        return 0.15

    def as_dict(self, now_ms: int) -> Dict[str, object]:
        return {
            "side": self.side, "price": self.price, "size": round(self.size, 6),
            "initial_size": round(self.initial_size, 6),
            "persistence_ms": self.persistence_ms, "updates": self.updates,
            "replenishments": self.replenishments, "reductions": self.reductions,
            "executed": round(self.executed, 6),
            "absorbed_share": round(self.absorbed_share, 4),
            "classification": self.classify(now_ms),
            "weight": round(self.weight(now_ms), 3),
        }


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
    alignment: Dict[str, float] = field(default_factory=dict)
    alignment_label: str = ""

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
            "alignment": dict(self.alignment),
            "alignment_label": self.alignment_label,
        }


def _price_of(item: Tuple[float, float]) -> float:
    """Sort key as a module function rather than a lambda: profiled at
    6.6 million calls for 3,000 deltas, where the lambda's own frame was
    a measurable share of the cost."""
    return item[0]


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
        self.resyncs = 0
        self.desync_reason = ""
        self._sorted_version: Optional[Tuple] = None
        self._sorted_bids: List[Tuple[float, float]] = []
        self._sorted_asks: List[Tuple[float, float]] = []
        self._pending_exec: Dict[str, float] = {"bid": 0.0, "ask": 0.0}
        self._flows: TimeSeries = TimeSeries(horizon_ms=FLOW_WINDOW_MS * 4)
        self._walls: Dict[Tuple[str, float], Wall] = {}
        self._wall_cancels: TimeSeries = TimeSeries(horizon_ms=60_000)
        self._spoofs: TimeSeries = TimeSeries(horizon_ms=60_000)

    # ---- ingest ----

    def reset(self, reason: str = "resync") -> None:
        """Throw the local book away.

        Called when the socket reconnected, when frames were dropped, or
        when the sequence broke. Everything derived from the book goes
        with it: half a book is not a smaller book, it is a wrong one.
        The counters survive, because they are the record of how often
        this had to happen."""
        self.bids.clear()
        self.asks.clear()
        self.synced = False
        self.last_update_id = None
        self.updated_at_ms = 0
        self.desync_reason = reason
        self.resyncs += 1
        self._pending_exec = {"bid": 0.0, "ask": 0.0}
        self._walls.clear()

    def sequence_stats(self) -> Dict[str, object]:
        """What the sequence actually did, for the health block."""
        return {
            "last_update_id": self.last_update_id,
            "sequence_gaps": self.gaps,
            "snapshots_received": self.snapshots,
            "deltas_received": self.deltas_applied,
            "resync_count": self.resyncs,
            "crossed_books": self.crossings,
            "synced": self.synced,
            "desync_reason": "" if self.synced else self.desync_reason,
        }

    def apply(self, message: Dict) -> bool:
        """Apply one orderbook message. False when it was not usable.

        A snapshot always applies and always re-syncs. A delta applies
        only when it continues the sequence; a genuine gap desyncs rather
        than being papered over, because a silently wrong book is the
        failure this whole check exists to prevent."""
        data = message.get("data") or {}
        kind = str(message.get("type") or "").lower()
        stamp = int(message.get("ts") or message.get("cts") or 0)
        try:
            update_id = int(data.get("u"))
        except (TypeError, ValueError):
            update_id = None

        if kind == "snapshot":
            return self._apply_snapshot(data, stamp, update_id)
        if kind != "delta":
            return False

        # Bybit documents a restart case: a DELTA carrying u == 1 is a
        # fresh snapshot, not a message from the middle of a sequence.
        # Read as a gap it desyncs the book and asks for a resync, and the
        # resubscribe that follows produces another u == 1 - which is a
        # resync loop that feeds itself. This is one of the causes of the
        # constant resyncing seen on the live feed.
        if update_id == 1:
            return self._apply_snapshot(data, stamp, update_id)

        return self._apply_delta(data, stamp, update_id)

    def _apply_snapshot(self, data: Dict, stamp: int,
                        update_id: Optional[int]) -> bool:
        self.bids.clear()
        self.asks.clear()
        self._merge(data)
        self.last_update_id = update_id
        self.snapshots += 1
        if self._crossed():
            self.crossings += 1
            self.synced = False
            self.desync_reason = "snapshot crossed"
            return False
        self.synced = True
        self.desync_reason = ""
        self.updated_at_ms = stamp or self.updated_at_ms
        self._pending_exec = {"bid": 0.0, "ask": 0.0}
        # Walls are registered from the snapshot too, not only from
        # deltas. Found by a test: a book that synced and then received
        # few updates had no walls at all, because the only call site was
        # in the delta branch - so a large resting order sitting untouched,
        # which is exactly the interesting case, was invisible until
        # something else moved.
        self._track_walls(stamp)
        return True

    def _apply_delta(self, data: Dict, stamp: int,
                     update_id: Optional[int]) -> bool:
        if not self.synced:
            return False                # nothing to apply a delta to yet

        if update_id is not None and self.last_update_id is not None:
            if update_id <= self.last_update_id:
                return False            # a repeat; harmless, and not an error
            missing = update_id - self.last_update_id - 1
            if missing > 0:
                # Frames were lost between there and here, so some levels
                # in the local book are stale and there is no way to tell
                # which. The book stops being trusted and says how much
                # it missed.
                self.gaps += 1
                self.synced = False
                self.desync_reason = (
                    f"sequence gap: expected {self.last_update_id + 1}, got "
                    f"{update_id} ({missing} frame(s) lost)")
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
            self.desync_reason = "crossed book after delta"
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

    def note_trade(self, taker_side: str, quantity: float,
                   price: Optional[float] = None) -> None:
        """Tell the book that liquidity was EXECUTED, not withdrawn.

        A taker Buy lifts offers, so it consumes the ASK side; a taker
        Sell hits bids. Without this the engine would read every filled
        order as a cancelled one and report constant 'pulling' in exactly
        the conditions - heavy trading - where pulling matters most.

        `price` is optional and is what lets a wall know whether it was
        traded through or pulled: volume printing at a wall's own price is
        attributed to it, and a wall that disappears having absorbed
        nothing while price never reached it is a spoof rather than a
        defender that gave way."""
        side = "ask" if str(taker_side).lower().startswith("b") else "bid"
        quantity = max(0.0, float(quantity))
        self._pending_exec[side] += quantity
        if price is None:
            return
        price = float(price)
        for (wall_side, wall_price), wall in self._walls.items():
            if wall_side != side:
                continue
            # "At this level" means within half a tick of it, and the tick
            # is inferred from the book rather than configured per symbol.
            if abs(wall_price - price) <= self._tick() * 0.5:
                wall.executed += quantity
                wall.reached = True

    def _tick(self) -> float:
        """The smallest gap between adjacent levels currently quoted.

        Inferred rather than configured: Bybit's tick size differs per
        instrument and this engine watches seven of them, so reading it
        off the book is both correct and free."""
        prices = sorted(self.bids)[-5:] + sorted(self.asks)[:5]
        gaps = [abs(b - a) for a, b in zip(prices, prices[1:]) if abs(b - a) > 0]
        return min(gaps) if gaps else 0.0

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
            for side, levels in (("bid", self.top_bids()), ("ask", self.top_asks())):
                for price, size in levels:
                    if size < threshold:
                        continue
                    key = (side, price)
                    existing = self._walls.get(key)
                    if existing is None:
                        live[key] = Wall(side, price, size, stamp, stamp)
                    else:
                        existing.observe(size, stamp)
                        live[key] = existing
        self._touch_walls()
        for key, wall in self._walls.items():
            if key in live:
                continue
            # Gone. Traded through, or pulled? If price never reached it
            # and nothing executed against it, someone withdrew it - which
            # is the shape of a spoof, and is counted as one.
            if not self._price_reached(wall) and wall.executed <= 0:
                self._wall_cancels.add(stamp, wall)
                self._spoofs.add(stamp, wall)
        self._walls = live

    def _touch_walls(self) -> None:
        """Record whether the market has actually come to each wall.

        A bid wall is reached when the OFFER falls to it - when someone is
        willing to sell at its price. Checked on every update and latched,
        because by the time a wall disappears the book has already moved
        and the question "did price ever get there" cannot be answered
        from the current state."""
        best_bid, best_ask = self.best_bid(), self.best_ask()
        for wall in self._walls.values():
            if wall.reached:
                continue
            if wall.side == "bid" and best_ask is not None and best_ask <= wall.price:
                wall.reached = True
            elif wall.side == "ask" and best_bid is not None and best_bid >= wall.price:
                wall.reached = True

    def _price_reached(self, wall: Wall) -> bool:
        """Whether the market ever came to this wall.

        The first version asked whether the best bid was at or below a bid
        wall's price - which is true the instant the wall is PULLED, since
        the touch then drops below it. So every pull looked like a wall
        that had been traded to, and no spoof was ever counted. Found by
        the test that expected one.

        The question is answered from the latch above and from executed
        volume, both of which are recorded while the wall still exists."""
        return wall.reached or wall.executed > 0

    # ---- views ----

    def _sorted_sides(self) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Both sides, ordered, sorted at most once per book version.

        `metrics()` asks for the top of the book about fifty times - OBI
        at five depths, weighted OBI, walls, absorption, stacking - and
        every one of those used to sort the whole book again. Profiled at
        141,000 sorts for 3,000 deltas, which was the single largest cost
        in the ingest path and the reason the consumer could not keep up
        with a 200 frame/second feed."""
        version = (self.last_update_id, self.updated_at_ms,
                   len(self.bids), len(self.asks))
        if self._sorted_version != version:
            self._sorted_bids = sorted(self.bids.items(), key=_price_of, reverse=True)
            self._sorted_asks = sorted(self.asks.items(), key=_price_of)
            self._sorted_version = version
        return self._sorted_bids, self._sorted_asks

    def top_bids(self, count: Optional[int] = None) -> List[Tuple[float, float]]:
        return self._sorted_sides()[0][:count or self.depth]

    def top_asks(self, count: Optional[int] = None) -> List[Tuple[float, float]]:
        return self._sorted_sides()[1][:count or self.depth]

    def side_totals(self) -> Tuple[float, float]:
        """Total resting size on each side. Cheap: no sort, no slice.

        Exists because the pre-break engine wants the two depths on every
        delta and used to get them by asking for the whole metric set."""
        return (sum(self.bids.values()), sum(self.asks.values()))

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
        out.alignment = self.alignment()
        out.alignment_label = self.alignment_label()
        out.bid_walls = sum(1 for (side, _), _ in self._walls.items() if side == "bid")
        out.ask_walls = sum(1 for (side, _), _ in self._walls.items() if side == "ask")
        out.max_wall_persistence_ms = max((w.persistence_ms for w in self._walls.values()),
                                          default=0)
        out.wall_cancellations = len(self._wall_cancels.window(60_000))
        return out

    # ---- alignment across depths ----

    def alignment(self) -> Dict[str, float]:
        """Whether the depths AGREE, not just what the touch says.

        The failure this exists to prevent: OBI1 at +0.98 with OBI50 at
        −0.12 and a weighted OBI of −0.03. That is not a strong long. It
        is one queue at the touch temporarily favouring bids while the
        book behind it leans the other way - which is frequently a
        precursor to the opposite move, and is certainly not accumulation.

        Three numbers:

          top_book_score   the first five levels, where the next hundred
                           milliseconds actually trade
          deep_book_score  levels 10 through 50, where size that intends
                           to stay sits
          consistency      how much the five depths agree with each other,
                           0 (they contradict) to 1 (unanimous)

        and `book_alignment`, which is the signed reading the score uses:
        the mean of the depths, multiplied by their consistency. A book
        that disagrees with itself scores near zero however extreme any
        one depth is, which is the whole point."""
        if not self.synced:
            return {"top_book_score": 0.0, "deep_book_score": 0.0,
                    "consistency": 0.0, "book_alignment": 0.0}

        depths = {depth: self.obi(depth) for depth in OBI_DEPTHS}
        top = (depths[1] + depths[5]) / 2.0
        deep_levels = [depths[10], depths[25], depths[50]]
        deep = sum(deep_levels) / len(deep_levels)

        values = list(depths.values())
        total = sum(abs(v) for v in values)
        consistency = (abs(sum(values)) / total) if total > 0 else 0.0

        # The mean across depths, then discounted by how much they agree.
        # Weighted toward the deep end on purpose: the touch is the
        # noisiest part of the book and the easiest to fake.
        mean = 0.35 * top + 0.65 * deep
        return {
            "top_book_score": round(clamp(top), 6),
            "deep_book_score": round(clamp(deep), 6),
            "consistency": round(clamp(consistency, 0.0, 1.0), 6),
            "book_alignment": round(clamp(mean * consistency), 6),
        }

    def alignment_label(self) -> str:
        """The alignment said in words, for the panel."""
        data = self.alignment()
        top, deep = data["top_book_score"], data["deep_book_score"]

        def side(value: float) -> str:
            if value > 0.15:
                return "BULLISH"
            if value < -0.15:
                return "BEARISH"
            return "NEUTRAL"

        top_side, deep_side = side(top), side(deep)
        if top_side == deep_side:
            return f"BOOK_{top_side}" if top_side != "NEUTRAL" else "BOOK_BALANCED"
        return f"TOP_BOOK_{top_side}_DEEP_BOOK_{deep_side}"

    # ---- walls, classified ----

    def wall_summary(self, now_ms: Optional[int] = None) -> Dict[str, object]:
        now_ms = now_ms or self.updated_at_ms
        walls = [wall.as_dict(now_ms) for wall in self._walls.values()]
        by_kind: Dict[str, int] = {}
        for wall in walls:
            kind = str(wall["classification"])
            by_kind[kind] = by_kind.get(kind, 0) + 1
        bid_weight = sum(float(w["weight"]) * float(w["size"])
                         for w in walls if w["side"] == "bid")
        ask_weight = sum(float(w["weight"]) * float(w["size"])
                         for w in walls if w["side"] == "ask")
        total = bid_weight + ask_weight
        raw_size = sum(float(w["size"]) for w in walls)

        # The per-wall weight CANCELS out of a ratio. With one transient
        # wall on the bid and nothing on the ask, (bid - ask) / total is
        # 0.15s / 0.15s = +1.0 - the maximum reading the feature can
        # produce, from a single flimsy order that has been there for
        # eight seconds. Wall.weight was doing its job and the ratio was
        # throwing the answer away.
        #
        # So the ratio gives the DIRECTION and a quality factor gives the
        # CONVICTION: the size-weighted mean of the per-wall weights, which
        # is 1.0 when the walls are persistent and 0.15 when the only wall
        # on the book is one that appeared a moment ago.
        direction = ((bid_weight - ask_weight) / total) if total > 0 else 0.0
        quality = (total / raw_size) if raw_size > 0 else 0.0
        return {
            "walls": sorted(walls, key=lambda w: -float(w["weight"]))[:10],
            "by_classification": by_kind,
            "bid_weighted_size": round(bid_weight, 4),
            "ask_weighted_size": round(ask_weight, 4),
            # -1..+1: which side's SERIOUS walls are heavier, damped by how
            # serious they actually are.
            "wall_bias": round(direction * quality, 6),
            "wall_direction": round(direction, 6),
            "wall_quality": round(quality, 4),
            "wall_count": len(walls),
            "spoofs_60s": len(self._spoofs.window(60_000)),
            "cancellations_60s": len(self._wall_cancels.window(60_000)),
        }

    def raw_absorption(self, window_ms: int = FLOW_WINDOW_MS) -> Dict[str, float]:
        """Absorption's raw inputs, for the normaliser to scale.

        Returned as the three quantities rather than as the ratio, so the
        caller can normalise against traded volume AND against depth -
        `added / executed` alone is an unbounded number that means
        different things at different activity levels."""
        bid_flow, ask_flow = self._flow_totals(window_ms)
        metrics_bid_depth = sum(size for _, size in self.top_bids())
        metrics_ask_depth = sum(size for _, size in self.top_asks())
        return {
            "bid_added": bid_flow.added, "bid_executed": bid_flow.executed,
            "ask_added": ask_flow.added, "ask_executed": ask_flow.executed,
            "bid_depth": metrics_bid_depth, "ask_depth": metrics_ask_depth,
        }

    def pressure_component(self) -> float:
        """One number on -1..+1 for the pressure score: how the resting
        book leans, across depths that agree. See `alignment`."""
        if not self.synced:
            return 0.0
        return clamp(self.alignment()["book_alignment"])
