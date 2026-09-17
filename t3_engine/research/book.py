"""The order book and tape, as the research code sees them.

This is a SECOND book implementation and that is deliberate. The live
engine's `orderbook_engine` is tuned for the live path - it keeps rolling
metrics, it tracks resync state, it feeds the scoring layers. What an
execution simulator needs is different and smaller: the exact resting
sizes at each price at a known instant, and the ability to walk them.
Sharing one object between the two would mean every change made for a
backtest had to be safe in production, which is how a backtest ends up
changing what the live engine does.

TWO CLOCKS, KEPT APART. `exchange_ms` is when Bybit says the book was
that way; `recv_ms` is when this process learned it. A decision may only
read `recv_ms` - it cannot know a thing before it arrives - while a fill
happens against `exchange_ms`, because that is when the liquidity was
really there. Collapsing them is the mistake that makes a backtest
profitable at zero latency.

WHAT AN AGGREGATED BOOK CANNOT TELL YOU. Bybit publishes size PER PRICE
LEVEL, not per order. 124.64 contracts at 5.759 might be one order or
forty, and nothing here can say where in that queue ours would sit. Every
maker fill downstream is therefore an estimate carrying an explicit
optimism parameter, never a fact.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

BUY = "Buy"
SELL = "Sell"


@dataclass(frozen=True)
class Trade:
    """One public trade. `side` is the AGGRESSOR's side, as Bybit sends
    it: "Buy" means a buyer lifted the ask, so it consumes ASK liquidity."""
    exchange_ms: int
    recv_ms: int
    price: float
    size: float
    side: str
    trade_id: str = ""

    @property
    def consumes(self) -> str:
        """Which side of the book this trade ate."""
        return "ask" if self.side == BUY else "bid"


@dataclass
class BookState:
    """A book at one instant. Prices are exact; sizes are aggregate."""
    exchange_ms: int = 0
    recv_ms: int = 0
    update_id: int = 0
    bids: List[Tuple[float, float]] = field(default_factory=list)   # desc by price
    asks: List[Tuple[float, float]] = field(default_factory=list)   # asc by price

    # ---- top of book ----

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid_size(self) -> float:
        return self.bids[0][1] if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        return self.asks[0][1] if self.asks else 0.0

    @property
    def mid(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2.0

    @property
    def microprice(self) -> Optional[float]:
        """Size-weighted toward the thinner side - the price the next
        trade is more likely to print at than the mid is."""
        if not self.bids or not self.asks:
            return None
        bid_p, bid_q = self.bids[0]
        ask_p, ask_q = self.asks[0]
        total = bid_q + ask_q
        if total <= 0:
            return self.mid
        return (bid_p * ask_q + ask_p * bid_q) / total

    @property
    def spread(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return self.asks[0][0] - self.bids[0][0]

    @property
    def spread_bps(self) -> Optional[float]:
        mid = self.mid
        spread = self.spread
        if mid is None or spread is None or mid <= 0:
            return None
        return 10_000.0 * spread / mid

    @property
    def crossed(self) -> bool:
        return bool(self.bids and self.asks and self.bids[0][0] >= self.asks[0][0])

    @property
    def valid(self) -> bool:
        return bool(self.bids and self.asks and not self.crossed)

    # ---- depth ----

    def size_at(self, side: str, price: float) -> float:
        levels = self.bids if side == "bid" else self.asks
        for level_price, size in levels:
            if abs(level_price - price) < 1e-12:
                return size
        return 0.0

    def depth_within_bps(self, side: str, bps: float) -> float:
        """Total resting size within `bps` of the mid, on one side."""
        mid = self.mid
        if mid is None:
            return 0.0
        limit = mid * bps / 10_000.0
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        for price, size in levels:
            if abs(price - mid) > limit:
                break
            total += size
        return total

    def walk(self, side: str, quantity: float) -> Tuple[float, float, int]:
        """Consume `quantity` from one side, level by level.

        Returns (average_price, filled_quantity, levels_touched). A
        partial result is a real outcome, not an error: a book that is
        thinner than the order is exactly the case a market order has to
        be priced against, and returning the mid instead is how a
        backtest hides its own slippage.
        """
        levels = self.asks if side == "ask" else self.bids
        remaining = float(quantity)
        notional = 0.0
        touched = 0
        for price, size in levels:
            if remaining <= 0:
                break
            take = min(remaining, size)
            notional += take * price
            remaining -= take
            touched += 1
        filled = float(quantity) - remaining
        if filled <= 0:
            return 0.0, 0.0, 0
        return notional / filled, filled, touched

    def imbalance(self, levels: int = 5) -> Optional[float]:
        """(bid - ask) / (bid + ask) over the top `levels`, in [-1, 1]."""
        bid = sum(size for _, size in self.bids[:levels])
        ask = sum(size for _, size in self.asks[:levels])
        total = bid + ask
        if total <= 0:
            return None
        return (bid - ask) / total

    def copy(self) -> "BookState":
        return BookState(exchange_ms=self.exchange_ms, recv_ms=self.recv_ms,
                         update_id=self.update_id, bids=list(self.bids),
                         asks=list(self.asks))


class BookReconstructor:
    """Bybit snapshot + deltas -> a `BookState` at every step.

    Bybit's rule, and the reason this is not a dict update: a delta with
    size "0" DELETES the level; any other size REPLACES it (it is not a
    increment). Getting that backwards leaves phantom liquidity in the
    book, which is exactly the liquidity a backtest would then fill
    against.
    """

    def __init__(self, depth_limit: int = 50) -> None:
        self.depth_limit = depth_limit
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self.synced = False
        self.last_update_id = 0
        self.desyncs = 0
        self.snapshots = 0
        self.deltas = 0
        self.desync_reason = ""

    def apply(self, frame: Dict) -> Optional[BookState]:
        """Feed one recorded orderbook frame. Returns the book after it,
        or None while the book is not usable."""
        kind = (frame.get("type") or "").lower()
        data = frame.get("data") or {}
        update_id = int(data.get("u") or 0)

        if kind == "snapshot":
            self._bids.clear()
            self._asks.clear()
            self._absorb(data)
            self.synced = True
            self.snapshots += 1
            self.last_update_id = update_id
            self.desync_reason = ""
        elif kind == "delta":
            if not self.synced:
                return None
            self.deltas += 1
            self._absorb(data)
            self.last_update_id = update_id
        else:
            return None

        state = self.state(int(frame.get("ts") or 0), int(frame.get("r") or 0))
        if state.crossed:
            # A crossed book is not a market, it is a reconstruction that
            # has gone wrong. Stop serving it rather than fill against it.
            self.synced = False
            self.desyncs += 1
            self.desync_reason = (f"crossed at u={update_id}: "
                                  f"bid {state.best_bid} >= ask {state.best_ask}")
            return None
        return state

    def _absorb(self, data: Dict) -> None:
        for price_str, size_str in (data.get("b") or []):
            price, size = float(price_str), float(size_str)
            if size == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = size
        for price_str, size_str in (data.get("a") or []):
            price, size = float(price_str), float(size_str)
            if size == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = size

    def state(self, exchange_ms: int = 0, recv_ms: int = 0) -> BookState:
        bids = sorted(self._bids.items(), key=lambda kv: -kv[0])[:self.depth_limit]
        asks = sorted(self._asks.items(), key=lambda kv: kv[0])[:self.depth_limit]
        return BookState(exchange_ms=exchange_ms, recv_ms=recv_ms,
                         update_id=self.last_update_id, bids=bids, asks=asks)

    def reset(self, reason: str = "") -> None:
        self._bids.clear()
        self._asks.clear()
        self.synced = False
        self.desync_reason = reason
