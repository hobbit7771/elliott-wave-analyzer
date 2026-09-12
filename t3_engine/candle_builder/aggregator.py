"""Causal candle aggregation.

Every timeframe the engine trades or confirms against - including the sub-
minute ones Binance does not stream natively (1s/5s/15s/30s) - is built here
from raw trade prints. The aggregator only ever looks at trades whose
timestamp has already happened: a bucket is "closed" strictly when a trade
(or an explicit `flush(now_ms)` call) proves time has moved past its
boundary. Nothing here ever peeks at a future trade to decide the current
bar's close - that would be the lookahead the spec explicitly forbids
(section 16).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe


@dataclass
class Trade:
    timestamp: int  # ms
    price: float
    quantity: float
    is_buyer_maker: bool  # True => taker was a SELLER (Binance convention)

    @property
    def taker_buy_qty(self) -> float:
        return 0.0 if self.is_buyer_maker else self.quantity


class TimeframeAggregator:
    """Builds candles for a single timeframe from a stream of trades."""

    def __init__(self, timeframe: Timeframe, on_closed_candle: Optional[Callable[[Candle], None]] = None):
        self.timeframe = timeframe
        self._interval_ms = timeframe.seconds * 1000
        self._on_closed_candle = on_closed_candle
        self._current: Optional[Candle] = None
        self.closed_candles: List[Candle] = []

    def _bucket_start(self, ts: int) -> int:
        return (ts // self._interval_ms) * self._interval_ms

    def on_trade(self, trade: Trade) -> Optional[Candle]:
        """Feed one trade. Returns the just-closed candle, if this trade
        rolled the bucket over."""
        bucket_open = self._bucket_start(trade.timestamp)
        bucket_close = bucket_open + self._interval_ms - 1

        closed = None
        if self._current is not None and bucket_open > self._current.open_time:
            closed = self._seal_current()

        if self._current is None:
            self._current = Candle(
                timeframe=self.timeframe,
                open_time=bucket_open,
                close_time=bucket_close,
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                volume=trade.quantity,
                taker_buy_volume=trade.taker_buy_qty,
                trades=1,
                closed=False,
            )
        else:
            c = self._current
            c.high = max(c.high, trade.price)
            c.low = min(c.low, trade.price)
            c.close = trade.price
            c.volume += trade.quantity
            c.taker_buy_volume += trade.taker_buy_qty
            c.trades += 1

        return closed

    def flush(self, now_ms: int) -> Optional[Candle]:
        """Force-close the current bucket if wall-clock time has moved past
        it, even with no new trade yet (needed for illiquid sub-second
        buckets so the pipeline doesn't stall waiting for a print)."""
        if self._current is None:
            return None
        if now_ms > self._current.close_time:
            return self._seal_current()
        return None

    def _seal_current(self) -> Candle:
        c = self._current
        c.closed = True
        self.closed_candles.append(c)
        self._current = None
        if self._on_closed_candle:
            self._on_closed_candle(c)
        return c

    @property
    def current_candle(self) -> Optional[Candle]:
        return self._current


class MultiTimeframeCandleBuilder:
    """Fans a single trade stream out to every timeframe the engine needs,
    plus resamples closed lower-TF candles up into higher TFs so we are not
    re-scanning the full trade tape for every timeframe."""

    def __init__(self, timeframes: List[Timeframe], on_closed_candle: Optional[Callable[[Timeframe, Candle], None]] = None):
        self._on_closed_candle = on_closed_candle
        self.aggregators: Dict[Timeframe, TimeframeAggregator] = {
            tf: TimeframeAggregator(tf, on_closed_candle=lambda c, tf=tf: self._emit(tf, c))
            for tf in timeframes
        }

    def _emit(self, tf: Timeframe, candle: Candle) -> None:
        if self._on_closed_candle:
            self._on_closed_candle(tf, candle)

    def on_trade(self, trade: Trade) -> None:
        for agg in self.aggregators.values():
            agg.on_trade(trade)

    def flush(self, now_ms: int) -> None:
        for agg in self.aggregators.values():
            agg.flush(now_ms)

    def closed_candles(self, tf: Timeframe) -> List[Candle]:
        return self.aggregators[tf].closed_candles

    def current_candle(self, tf: Timeframe) -> Optional[Candle]:
        return self.aggregators[tf].current_candle


def resample_candles(candles: List[Candle], target: Timeframe) -> List[Candle]:
    """Resample a list of already-CLOSED lower-timeframe candles into a
    higher timeframe. Used by the historical loader to build e.g. 5m bars
    out of 1m REST klines when a native 5m fetch would waste request
    weight, and by the backtester. Purely causal: bucket N only ever
    contains source candles that closed before or at bucket N's close."""
    if not candles:
        return []
    interval_ms = target.seconds * 1000
    out: List[Candle] = []
    bucket: List[Candle] = []
    bucket_open = None

    def seal():
        if not bucket:
            return
        out.append(Candle(
            timeframe=target,
            open_time=bucket_open,
            close_time=bucket_open + interval_ms - 1,
            open=bucket[0].open,
            high=max(c.high for c in bucket),
            low=min(c.low for c in bucket),
            close=bucket[-1].close,
            volume=sum(c.volume for c in bucket),
            taker_buy_volume=sum(c.taker_buy_volume for c in bucket),
            trades=sum(c.trades for c in bucket),
            closed=True,
        ))

    for c in candles:
        b_open = (c.open_time // interval_ms) * interval_ms
        if bucket_open is None:
            bucket_open = b_open
        if b_open != bucket_open:
            seal()
            bucket = []
            bucket_open = b_open
        bucket.append(c)
    seal()
    return out
