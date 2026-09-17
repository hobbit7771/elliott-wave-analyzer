"""Causal microstructure features.

CAUSAL MEANS ONE THING HERE: every window is closed at the RECEIVE time
of the event being processed, and nothing reads an element stamped later
than that. A feature that peeks one frame ahead makes every strategy
built on it profitable and none of them real, and the peek is usually
invisible - an "average over the last N events" computed after appending
the current one is already a peek if the current one is the trigger.

So the order is fixed: features are read, THEN the event is folded in.
`update()` returns the view as it was BEFORE the event, and the strategy
sees that. `MarketView.decided_at_ms` is a receive time for the same
reason - it is the stamp an order may be sent with.

WHAT EACH FEATURE CLAIMS.

  taker_delta      signed aggressive volume over the window, normalised
                   by the window's total. In [-1, 1]. +1 means every
                   trade was a buyer lifting the ask.
  ofi              order-flow imbalance in the Cont sense: additions to
                   the bid and removals from the ask are positive
                   pressure. Computed from the book's own changes, so it
                   sees intent that never printed as a trade.
  depth_drain      how much of the opposing side vanished over the
                   window, as a fraction of what was there. The feature
                   that says a level is failing before the price moves.
  replenishment    size added back at the touched level after a trade
                   ate it. High replenishment is a defended level, which
                   is a reason NOT to trade a break.
  microprice_edge  (microprice - mid) in bps. Where the next print
                   leans.
  intensity        trades per second over the window. Separates a real
                   impulse from a quiet tape.
  realised_vol_bps standard deviation of mid returns over the window, in
                   bps. The scale everything else has to be judged
                   against: a 5bps signal in a 50bps market is noise.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from t3_engine.research.book import BUY, SELL, BookState, Trade


@dataclass
class MarketView:
    """Everything a strategy may look at, and the stamp it may act on."""
    symbol: str
    decided_at_ms: int                 # RECEIVE time: what an order carries
    exchange_ms: int                   # when the venue says it was so
    book: Optional[BookState] = None

    taker_delta: float = 0.0
    taker_volume: float = 0.0
    ofi: float = 0.0
    bid_drain: float = 0.0
    ask_drain: float = 0.0
    bid_replenishment: float = 0.0
    ask_replenishment: float = 0.0
    microprice_edge_bps: float = 0.0
    imbalance: float = 0.0
    intensity: float = 0.0
    realised_vol_bps: float = 0.0
    spread_bps: float = 0.0
    trend_bps: float = 0.0             # mid move over the window, in bps
    liquidation_notional: float = 0.0
    liquidation_side: str = ""
    samples: int = 0
    warm: bool = False

    @property
    def mid(self) -> Optional[float]:
        return self.book.mid if self.book else None

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol, "decided_at_ms": self.decided_at_ms,
            "exchange_ms": self.exchange_ms,
            "mid": self.mid, "spread_bps": round(self.spread_bps, 4),
            "taker_delta": round(self.taker_delta, 4),
            "taker_volume": round(self.taker_volume, 4),
            "ofi": round(self.ofi, 4),
            "bid_drain": round(self.bid_drain, 4),
            "ask_drain": round(self.ask_drain, 4),
            "bid_replenishment": round(self.bid_replenishment, 4),
            "ask_replenishment": round(self.ask_replenishment, 4),
            "microprice_edge_bps": round(self.microprice_edge_bps, 4),
            "imbalance": round(self.imbalance, 4),
            "intensity": round(self.intensity, 3),
            "realised_vol_bps": round(self.realised_vol_bps, 4),
            "trend_bps": round(self.trend_bps, 4),
            "liquidation_notional": round(self.liquidation_notional, 2),
            "samples": self.samples, "warm": self.warm,
        }


class FeatureEngine:
    """Rolling, causal, and cheap enough for a free-tier box.

    Every window is a deque trimmed by receive time, so the cost per
    event is amortised constant and no historical array is rebuilt on a
    tick - the constraint the hosting actually imposes."""

    def __init__(self, symbol: str, *, window_ms: int = 3_000,
                 vol_window_ms: int = 30_000, depth_bps: float = 10.0,
                 min_samples: int = 20) -> None:
        self.symbol = symbol.upper()
        self.window_ms = window_ms
        self.vol_window_ms = vol_window_ms
        self.depth_bps = depth_bps
        self.min_samples = min_samples

        self._trades: Deque[Tuple[int, float, float, str]] = deque()   # t, price, size, side
        self._mids: Deque[Tuple[int, float]] = deque()
        self._depths: Deque[Tuple[int, float, float]] = deque()        # t, bid, ask
        self._liquidations: Deque[Tuple[int, float, str]] = deque()
        self._ofi: Deque[Tuple[int, float]] = deque()

        self.book: Optional[BookState] = None
        self._previous_book: Optional[BookState] = None
        self._samples = 0

    # ---- reading, then folding ------------------------------------------

    def view(self, decided_at_ms: int, exchange_ms: int) -> MarketView:
        """The state as it is NOW, before the current event is folded in."""
        self._trim(decided_at_ms)
        view = MarketView(symbol=self.symbol, decided_at_ms=int(decided_at_ms),
                          exchange_ms=int(exchange_ms), book=self.book,
                          samples=self._samples,
                          warm=self._samples >= self.min_samples)

        buys = sum(size for _, _, size, side in self._trades if side == BUY)
        sells = sum(size for _, _, size, side in self._trades if side == SELL)
        total = buys + sells
        view.taker_volume = total
        view.taker_delta = (buys - sells) / total if total > 0 else 0.0
        view.intensity = (len(self._trades) / (self.window_ms / 1000.0)
                          if self.window_ms else 0.0)
        view.ofi = sum(value for _, value in self._ofi)

        if self._depths:
            first_bid, first_ask = self._depths[0][1], self._depths[0][2]
            last_bid, last_ask = self._depths[-1][1], self._depths[-1][2]
            view.bid_drain = ((first_bid - last_bid) / first_bid
                              if first_bid > 0 else 0.0)
            view.ask_drain = ((first_ask - last_ask) / first_ask
                              if first_ask > 0 else 0.0)
            peak_bid = max(d[1] for d in self._depths)
            peak_ask = max(d[2] for d in self._depths)
            trough_bid = min(d[1] for d in self._depths)
            trough_ask = min(d[2] for d in self._depths)
            view.bid_replenishment = ((last_bid - trough_bid) / peak_bid
                                      if peak_bid > 0 else 0.0)
            view.ask_replenishment = ((last_ask - trough_ask) / peak_ask
                                      if peak_ask > 0 else 0.0)

        if self.book is not None and self.book.valid:
            mid = self.book.mid
            micro = self.book.microprice
            if mid and micro:
                view.microprice_edge_bps = 10_000.0 * (micro - mid) / mid
            view.imbalance = self.book.imbalance(5) or 0.0
            view.spread_bps = self.book.spread_bps or 0.0

        if len(self._mids) >= 3:
            prices = [p for _, p in self._mids]
            returns = [10_000.0 * (prices[i + 1] - prices[i]) / prices[i]
                       for i in range(len(prices) - 1) if prices[i] > 0]
            if len(returns) >= 2:
                view.realised_vol_bps = statistics.pstdev(returns)
            if prices[0] > 0:
                view.trend_bps = 10_000.0 * (prices[-1] - prices[0]) / prices[0]

        if self._liquidations:
            view.liquidation_notional = sum(n for _, n, _ in self._liquidations)
            view.liquidation_side = self._liquidations[-1][2]
        return view

    def on_book(self, book: BookState) -> None:
        stamp = book.recv_ms or book.exchange_ms
        if book.valid:
            if self._previous_book is not None and self._previous_book.valid:
                self._ofi.append((stamp, _ofi_step(self._previous_book, book)))
            self._mids.append((stamp, book.mid))
            self._depths.append((stamp,
                                 book.depth_within_bps("bid", self.depth_bps),
                                 book.depth_within_bps("ask", self.depth_bps)))
            self._previous_book = book
            self.book = book
            self._samples += 1
        self._trim(stamp)

    def on_trade(self, trade: Trade) -> None:
        stamp = trade.recv_ms or trade.exchange_ms
        self._trades.append((stamp, trade.price, trade.size, trade.side))
        self._trim(stamp)

    def on_liquidation(self, at_ms: int, notional: float, side: str) -> None:
        self._liquidations.append((int(at_ms), float(notional), side))
        self._trim(at_ms)

    def _trim(self, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        for series in (self._trades, self._depths, self._ofi, self._liquidations):
            while series and series[0][0] < cutoff:
                series.popleft()
        vol_cutoff = now_ms - self.vol_window_ms
        while self._mids and self._mids[0][0] < vol_cutoff:
            self._mids.popleft()


def _ofi_step(previous: BookState, current: BookState) -> float:
    """Cont's order-flow imbalance over one book update.

    On the bid: a higher best price, or more size at the same price, is
    buying pressure; a lower best price wipes it. The ask is the mirror.
    It reads intent that never printed as a trade, which is the point."""
    value = 0.0
    p_bid, p_bid_q = previous.bids[0]
    c_bid, c_bid_q = current.bids[0]
    if c_bid > p_bid:
        value += c_bid_q
    elif c_bid < p_bid:
        value -= p_bid_q
    else:
        value += c_bid_q - p_bid_q

    p_ask, p_ask_q = previous.asks[0]
    c_ask, c_ask_q = current.asks[0]
    if c_ask < p_ask:
        value -= c_ask_q
    elif c_ask > p_ask:
        value += p_ask_q
    else:
        value -= c_ask_q - p_ask_q
    return value
