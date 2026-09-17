"""An event-driven execution simulator that can say no.

THE ONE RULE EVERYTHING ELSE SERVES: an order may only ever interact
with liquidity that existed AFTER it could have arrived. A backtest that
fills at the signal's own price is not measuring a strategy, it is
measuring the absence of a delay. So every order carries four separate
times and they are not the same number:

  decided_at   the process learned enough to want this order. Bounded by
               RECEIVE time - a decision cannot read a book it has not
               been sent yet.
  arrives_at   decided_at + send latency. The first instant the exchange
               could match it.
  acked_at     arrives_at + ack latency. When we learn it is live.
  cancel lands arrives at its own latency, and the window between asking
               and landing is a window in which the order can still fill.
               Assuming a cancel is instant is how a simulated strategy
               avoids every loss it would really have taken.

FILLS, AND WHAT IS AN ESTIMATE.

  Taker fills walk the real book, level by level, as of arrival. If the
  book is thinner than the order, the fill is PARTIAL - that is a real
  outcome, not an error, and returning the mid instead is how a backtest
  hides its own slippage.

  Maker fills are ESTIMATES and the code never pretends otherwise. Bybit
  publishes aggregate size per price level, so where our order sits in
  the queue at that level is unknowable. The model is:

    on arrival, assume `queue_factor` of the resting size is ahead of us
      (1.0 = all of it, the conservative reading; 0.0 = we are first,
       which nobody should believe)
    aggressive trades at our price reduce what is ahead of us, and the
      excess fills us
    a trade THROUGH our price means the level was swept: we fill
    size vanishing from our level without a trade is CANCELLATION, and
      the conservative reading is that it was behind us, so it does not
      advance us at all - `cancel_helps` opens the other reading

  A strategy that is profitable only at queue_factor 0.3 has not been
  shown to work; it has been shown to need a queue position nobody
  measured. Both readings are reported so that difference is visible.

  A post-only order that is MARKETABLE WHEN IT ARRIVES is rejected, not
  quietly filled as a maker. That is what the venue does, and simulating
  it the other way hands the strategy a taker fill at a maker fee.

SELF-IMPACT IS NOT MODELLED, AND THAT IS A LIMIT. Replaying recorded
data cannot show how the market would have answered our order. Sizes are
therefore capped against observed liquidity and the same resting size is
never handed to two of our own orders at once, but a large order's real
effect on the book is outside what this data can support.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from t3_engine.research.book import BUY, SELL, BookState, Trade
from t3_engine.research.costs import MAKER, TAKER, CostModel, FeeSchedule

# Order kinds
LIMIT = "limit"
MARKET = "market"

# Order states
PENDING = "pending"          # sent, not yet arrived at the exchange
OPEN = "open"                # resting
FILLED = "filled"
PARTIAL = "partially_filled"
CANCELLED = "cancelled"
REJECTED = "rejected"

TERMINAL = (FILLED, CANCELLED, REJECTED)


@dataclass(frozen=True)
class InstrumentSpec:
    """The venue's rounding rules. An order that ignores them is an order
    the exchange would have rejected, so a backtest that fills it is
    counting a trade that could not have happened."""
    symbol: str
    tick_size: float = 0.0001
    qty_step: float = 0.1
    min_qty: float = 0.1
    min_notional: float = 5.0

    def round_price(self, price: float, side: str, aggressive: bool = False) -> float:
        """Round to a tick. A resting BUY rounds DOWN and a resting SELL
        rounds UP, so rounding never makes a passive order cross."""
        ticks = price / self.tick_size
        if aggressive:
            rounded = round(ticks)
        elif side == BUY:
            rounded = math.floor(ticks + 1e-9)
        else:
            rounded = math.ceil(ticks - 1e-9)
        return round(rounded * self.tick_size, 12)

    def round_qty(self, qty: float) -> float:
        """Always DOWN: rounding a size up sends more than was intended."""
        steps = math.floor(qty / self.qty_step + 1e-9)
        return round(steps * self.qty_step, 12)

    def acceptable(self, price: float, qty: float) -> Tuple[bool, str]:
        if qty < self.min_qty - 1e-12:
            return False, f"qty {qty} below min_qty {self.min_qty}"
        if price * qty < self.min_notional - 1e-9:
            return False, (f"notional {price * qty:.4f} below "
                           f"min_notional {self.min_notional}")
        return True, ""


@dataclass(frozen=True)
class LatencyModel:
    """Four delays, because they are four different things.

    `information_ms` is already baked into recorded data (each frame
    carries the receive time), so it defaults to zero here and is applied
    by the feed rather than twice. The rest are the order's own round
    trip and are NOT measurable from a public feed - a public websocket's
    latency says nothing about a private order endpoint's. They default
    to values a Render Free box could plausibly achieve and every result
    is stress-tested against worse ones."""
    send_ms: float = 120.0
    ack_ms: float = 60.0
    cancel_ms: float = 120.0
    information_ms: float = 0.0

    def worse_by(self, factor: float) -> "LatencyModel":
        return LatencyModel(send_ms=self.send_ms * factor,
                            ack_ms=self.ack_ms * factor,
                            cancel_ms=self.cancel_ms * factor,
                            information_ms=self.information_ms * factor)


@dataclass(frozen=True)
class QueueModel:
    """How a resting order's place in the queue is guessed.

    queue_factor  fraction of the size already at our price that we
                  assume sits AHEAD of us. 1.0 is the conservative
                  reading and the default.
    cancel_helps  whether size disappearing without a trade advances us.
                  False is conservative: assume the cancels were behind
                  us, so they buy us nothing.
    """
    queue_factor: float = 1.0
    cancel_helps: bool = False

    @staticmethod
    def conservative() -> "QueueModel":
        return QueueModel(queue_factor=1.0, cancel_helps=False)

    @staticmethod
    def optimistic() -> "QueueModel":
        return QueueModel(queue_factor=0.5, cancel_helps=True)


@dataclass
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    price: float
    qty: float
    liquidity: str           # MAKER or TAKER
    at_ms: int
    fee: float = 0.0

    @property
    def notional(self) -> float:
        return self.price * self.qty

    def as_dict(self) -> Dict[str, object]:
        return {"fill_id": self.fill_id, "order_id": self.order_id,
                "symbol": self.symbol, "side": self.side, "price": self.price,
                "qty": self.qty, "liquidity": self.liquidity,
                "at_ms": self.at_ms, "fee": round(self.fee, 10)}


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str                # BUY / SELL
    kind: str                # LIMIT / MARKET
    qty: float
    price: Optional[float] = None
    post_only: bool = False
    reduce_only: bool = False
    decided_at_ms: int = 0
    arrives_at_ms: int = 0
    acked_at_ms: int = 0
    ttl_ms: Optional[int] = None
    tag: str = ""

    state: str = PENDING
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fills: List[Fill] = field(default_factory=list)
    reject_reason: str = ""
    cancel_requested_at_ms: Optional[int] = None
    cancel_lands_at_ms: Optional[int] = None
    closed_at_ms: Optional[int] = None

    # Maker bookkeeping
    queue_ahead: float = 0.0
    level_size_at_arrival: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def as_dict(self) -> Dict[str, object]:
        return {
            "order_id": self.order_id, "symbol": self.symbol, "side": self.side,
            "kind": self.kind, "qty": self.qty, "price": self.price,
            "post_only": self.post_only, "state": self.state,
            "filled_qty": round(self.filled_qty, 10),
            "avg_price": round(self.avg_price, 10) if self.avg_price else 0.0,
            "decided_at_ms": self.decided_at_ms, "arrives_at_ms": self.arrives_at_ms,
            "acked_at_ms": self.acked_at_ms, "closed_at_ms": self.closed_at_ms,
            "reject_reason": self.reject_reason, "tag": self.tag,
            "fills": [f.as_dict() for f in self.fills],
        }


class ExecutionSimulator:
    """Drive with market events in EXCHANGE-TIME order.

    The caller submits orders stamped with the RECEIVE time of whatever
    it based the decision on. The simulator adds the send latency, and
    from then on the order lives in exchange time like everything else.
    """

    def __init__(self, spec: InstrumentSpec, *,
                 latency: Optional[LatencyModel] = None,
                 queue: Optional[QueueModel] = None,
                 fees: Optional[FeeSchedule] = None,
                 max_participation: float = 0.25) -> None:
        self.spec = spec
        self.latency = latency or LatencyModel()
        self.queue = queue or QueueModel.conservative()
        self.costs = CostModel(fees)
        # Never take more than this share of the size resting at a level:
        # replayed data cannot show the market reacting to us, so a fill
        # that eats a whole level is a fill we cannot defend.
        self.max_participation = max_participation

        self.book: Optional[BookState] = None
        self.orders: Dict[str, Order] = {}
        self.open_orders: List[Order] = []
        self.pending: List[Order] = []
        self.fills: List[Fill] = []
        self._ids = itertools.count(1)
        self._fill_ids = itertools.count(1)
        self.now_ms = 0
        self.rejected_post_only = 0
        self.cancelled_after_fill = 0

    # ---- submission ----------------------------------------------------

    def submit(self, side: str, qty: float, *, kind: str = LIMIT,
               price: Optional[float] = None, post_only: bool = False,
               decided_at_ms: int, ttl_ms: Optional[int] = None,
               tag: str = "", reduce_only: bool = False) -> Order:
        """Queue an order for arrival. `decided_at_ms` must be a RECEIVE
        time: it is the moment the strategy knew enough, and the send
        latency is added to it here."""
        qty = self.spec.round_qty(qty)
        order = Order(
            order_id=f"o{next(self._ids)}", symbol=self.spec.symbol, side=side,
            kind=kind, qty=qty, post_only=post_only, reduce_only=reduce_only,
            decided_at_ms=int(decided_at_ms),
            arrives_at_ms=int(decided_at_ms + self.latency.send_ms),
            acked_at_ms=int(decided_at_ms + self.latency.send_ms + self.latency.ack_ms),
            ttl_ms=ttl_ms, tag=tag)
        if price is not None:
            order.price = self.spec.round_price(price, side,
                                                aggressive=(kind == MARKET))
        self.orders[order.order_id] = order

        if qty <= 0:
            order.state = REJECTED
            order.reject_reason = "quantity rounds to zero at the instrument's step"
            order.closed_at_ms = order.arrives_at_ms
            return order
        # Notional is only checkable against a price. A market order sent
        # with no book has none, so the check waits for arrival rather
        # than rejecting with a misleading "notional 0.0" - the real
        # reason there is that there is no market.
        reference = order.price or (self.book.mid if self.book and self.book.valid
                                    else None)
        if reference is not None:
            ok, why = self.spec.acceptable(reference, qty)
            if not ok:
                order.state = REJECTED
                order.reject_reason = why
                order.closed_at_ms = order.arrives_at_ms
                return order

        self.pending.append(order)
        return order

    def cancel(self, order_id: str, requested_at_ms: int) -> None:
        """Ask for a cancel. It LANDS later, and the order can still fill
        in between - which is the loss a strategy really takes when it
        tries to pull a quote into a move."""
        order = self.orders.get(order_id)
        if order is None or order.terminal:
            return
        if order.cancel_requested_at_ms is not None:
            return
        order.cancel_requested_at_ms = int(requested_at_ms)
        order.cancel_lands_at_ms = int(requested_at_ms + self.latency.cancel_ms)

    # ---- the event loop -------------------------------------------------

    def on_book(self, book: BookState) -> None:
        """A new book state, in exchange time."""
        previous = self.book
        self.now_ms = book.exchange_ms or self.now_ms
        self._advance(self.now_ms, book)
        self.book = book
        self._apply_book_change(previous, book)
        self._expire(self.now_ms)

    def on_trade(self, trade: Trade) -> None:
        """A public trade, in exchange time. This is what fills makers."""
        self.now_ms = trade.exchange_ms or self.now_ms
        self._advance(self.now_ms, self.book)
        self._match_makers(trade)
        self._expire(self.now_ms)

    def _advance(self, to_ms: int, book: Optional[BookState]) -> None:
        """Land every order action whose time has come."""
        still_pending = []
        for order in self.pending:
            if order.arrives_at_ms <= to_ms:
                self._arrive(order, book)
            else:
                still_pending.append(order)
        self.pending = still_pending

        for order in list(self.open_orders):
            if (order.cancel_lands_at_ms is not None
                    and order.cancel_lands_at_ms <= to_ms and not order.terminal):
                self._close(order, CANCELLED, to_ms)

    def _arrive(self, order: Order, book: Optional[BookState]) -> None:
        if book is None or not book.valid:
            order.state = REJECTED
            order.reject_reason = "no usable book at arrival"
            order.closed_at_ms = order.arrives_at_ms
            return

        if order.kind == MARKET:
            self._take(order, book, order.arrives_at_ms)
            return

        marketable = ((order.side == BUY and order.price >= (book.best_ask or math.inf))
                      or (order.side == SELL and order.price <= (book.best_bid or 0.0)))
        if marketable:
            if order.post_only:
                # The venue rejects it. Filling it as a maker here would
                # hand the strategy a taker's fill at a maker's fee.
                order.state = REJECTED
                order.reject_reason = ("post-only order was marketable on arrival "
                                       f"(price {order.price}, best "
                                       f"{book.best_ask if order.side == BUY else book.best_bid})")
                order.closed_at_ms = order.arrives_at_ms
                self.rejected_post_only += 1
                return
            self._take(order, book, order.arrives_at_ms, limit_price=order.price)
            if order.remaining <= 0:
                return

        # Rests. Its place in the queue is guessed HERE, from the size
        # that was at the level when it landed.
        side_key = "bid" if order.side == BUY else "ask"
        resting = book.size_at(side_key, order.price)
        order.level_size_at_arrival = resting
        order.queue_ahead = resting * self.queue.queue_factor
        order.state = OPEN
        self.open_orders.append(order)

    # ---- taker ----------------------------------------------------------

    def _take(self, order: Order, book: BookState, at_ms: int,
              limit_price: Optional[float] = None) -> None:
        """Walk the book. Partial is a real answer."""
        side_key = "ask" if order.side == BUY else "bid"
        levels = book.asks if side_key == "ask" else book.bids
        remaining = order.remaining
        taken: List[Tuple[float, float]] = []
        for price, size in levels:
            if remaining <= 1e-12:
                break
            if limit_price is not None:
                if order.side == BUY and price > limit_price + 1e-12:
                    break
                if order.side == SELL and price < limit_price - 1e-12:
                    break
            available = size * self.max_participation
            take = min(remaining, available)
            if take <= 0:
                continue
            taken.append((price, take))
            remaining -= take

        for price, qty in taken:
            self._record_fill(order, price, qty, TAKER, at_ms)

        if order.remaining <= 1e-12:
            self._close(order, FILLED, at_ms)
        elif order.kind == MARKET:
            # Nothing left to cross into. The unfilled part simply does
            # not happen; pretending otherwise invents liquidity.
            self._close(order, PARTIAL if order.filled_qty > 0 else CANCELLED, at_ms)

    # ---- maker ----------------------------------------------------------

    def _match_makers(self, trade: Trade) -> None:
        """A public trade at or through a resting order's price.

        `trade.side` is the AGGRESSOR. A Sell aggressor eats BIDS, so it
        is what can fill our resting buy."""
        eats = "bid" if trade.side == SELL else "ask"
        for order in list(self.open_orders):
            if order.terminal or order.remaining <= 0:
                continue
            ours = "bid" if order.side == BUY else "ask"
            if ours != eats:
                continue

            through = ((order.side == BUY and trade.price < order.price - 1e-12)
                       or (order.side == SELL and trade.price > order.price + 1e-12))
            at_our_price = abs(trade.price - order.price) <= 1e-12

            if through:
                # The tape printed past our level, so everything resting
                # at it - including us - was taken.
                self._record_fill(order, order.price, order.remaining, MAKER,
                                  trade.exchange_ms)
                self._close(order, FILLED, trade.exchange_ms)
                continue
            if not at_our_price:
                continue

            volume = trade.size
            if order.queue_ahead > 0:
                eaten = min(order.queue_ahead, volume)
                order.queue_ahead -= eaten
                volume -= eaten
            if volume <= 1e-12:
                continue
            qty = min(order.remaining, volume)
            self._record_fill(order, order.price, qty, MAKER, trade.exchange_ms)
            if order.remaining <= 1e-12:
                self._close(order, FILLED, trade.exchange_ms)

    def _apply_book_change(self, previous: Optional[BookState],
                           book: BookState) -> None:
        """Size leaving our level without a trade is CANCELLATION.

        The conservative reading is that those cancels were behind us, so
        they advance us not at all. `cancel_helps` opens the other one,
        pro-rata, and the difference between the two results is the
        honest width of what an aggregated book can tell us."""
        if previous is None or not self.queue.cancel_helps:
            return
        for order in self.open_orders:
            if order.terminal or order.price is None:
                continue
            side_key = "bid" if order.side == BUY else "ask"
            before = previous.size_at(side_key, order.price)
            after = book.size_at(side_key, order.price)
            if after >= before or before <= 0:
                continue
            removed = before - after
            share = min(1.0, removed / before)
            order.queue_ahead = max(0.0, order.queue_ahead * (1.0 - share))

    # ---- bookkeeping ----------------------------------------------------

    def _record_fill(self, order: Order, price: float, qty: float,
                     liquidity: str, at_ms: int) -> Fill:
        qty = min(qty, order.remaining)
        fee_bps = self.costs.fees.side_bps(liquidity)
        fee = price * qty * fee_bps / 10_000.0
        fill = Fill(fill_id=f"f{next(self._fill_ids)}", order_id=order.order_id,
                    symbol=order.symbol, side=order.side, price=price, qty=qty,
                    liquidity=liquidity, at_ms=int(at_ms), fee=fee)
        notional = order.avg_price * order.filled_qty + price * qty
        order.filled_qty += qty
        order.avg_price = notional / order.filled_qty if order.filled_qty else 0.0
        order.fills.append(fill)
        self.fills.append(fill)
        if order.state == OPEN and order.remaining > 0:
            order.state = PARTIAL
        return fill

    def _close(self, order: Order, state: str, at_ms: int) -> None:
        if order.cancel_requested_at_ms is not None and state == FILLED:
            # It filled while the cancel was in flight. This is the loss a
            # strategy really takes when it tries to pull a quote.
            self.cancelled_after_fill += 1
        order.state = state
        order.closed_at_ms = int(at_ms)
        if order in self.open_orders:
            self.open_orders.remove(order)

    def _expire(self, now_ms: int) -> None:
        for order in list(self.open_orders):
            if order.ttl_ms is None or order.terminal:
                continue
            if now_ms >= order.acked_at_ms + order.ttl_ms:
                self.cancel(order.order_id, now_ms)

    # ---- reporting -------------------------------------------------------

    def stats(self) -> Dict[str, object]:
        placed = [o for o in self.orders.values()]
        maker_fills = [f for f in self.fills if f.liquidity == MAKER]
        taker_fills = [f for f in self.fills if f.liquidity == TAKER]
        filled = [o for o in placed if o.filled_qty > 0]
        return {
            "orders": len(placed),
            "filled_orders": len(filled),
            "fill_rate": round(len(filled) / len(placed), 4) if placed else 0.0,
            "partial_orders": sum(1 for o in placed
                                  if 0 < o.filled_qty < o.qty - 1e-12),
            "cancelled": sum(1 for o in placed if o.state == CANCELLED),
            "rejected": sum(1 for o in placed if o.state == REJECTED),
            "rejected_post_only": self.rejected_post_only,
            "filled_while_cancelling": self.cancelled_after_fill,
            "maker_fills": len(maker_fills),
            "taker_fills": len(taker_fills),
            "maker_share": round(len(maker_fills) / len(self.fills), 4)
            if self.fills else 0.0,
            "fees_paid": round(sum(f.fee for f in self.fills), 8),
        }
