"""Candles built locally from the trade stream.

Why this exists rather than relying on the `kline.*` topics: Bybit's kline
topic pushes the CURRENT bar on subscribe, not history. An engine that
took its structure only from that stream would have no swings for the
first several minutes of every process - and swings are what the level
tracker, the SMC state and the wave context are all built from, so the
pre-break score would be exactly zero until enough bars had accumulated.
A replay of a short capture has the same problem permanently, which is
how it was found.

So trades are aggregated here as well. The exchange's klines stay
authoritative where they exist - they include trades from before this
process connected, and they are what a chart would show - and these fill
the gap. `SymbolState` uses whichever series has more CLOSED bars.

A bar closes when a trade arrives stamped in a later bucket. That is the
only close condition: closing on a timer would mean a quiet market
produced bars that no trade ever confirmed, and closing the newest bar
speculatively is the lookahead every structure calculation here refuses.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from t3_engine.lead_engine.smc_engine import Candle
from t3_engine.lead_engine.trade_flow import Trade

# One minute, matching STRUCTURE_INTERVAL in state.py.
DEFAULT_INTERVAL_MS = 60_000

# How many bars each builder keeps. 1,500 is the brief's number and it is
# the right one for the level tracker: at the 15-second interval it uses,
# 400 bars was a hundred minutes of history, which on a quiet instrument
# is not enough range to contain a confirmed swing on both sides of the
# current price. 1,500 bars is a little over six hours there, and a day
# at the one-minute interval the structure engine uses. The cost is a few
# thousand small objects per symbol.
MAX_CANDLES = 1_500


class CandleBuilder:
    """Trades in, closed candles out. Causal by construction."""

    def __init__(self, symbol: str, interval_ms: int = DEFAULT_INTERVAL_MS) -> None:
        self.symbol = symbol.upper()
        self.interval_ms = int(interval_ms)
        self.candles: List[Candle] = []
        self._open: Optional[Dict[str, float]] = None
        self._bucket: Optional[int] = None

    def add(self, trade: Trade) -> Optional[Candle]:
        """Returns the candle that just CLOSED, if this trade closed one."""
        bucket = (trade.timestamp_ms // self.interval_ms) * self.interval_ms
        closed: Optional[Candle] = None

        if self._bucket is not None and bucket > self._bucket:
            closed = self._finish()
        elif self._bucket is not None and bucket < self._bucket:
            # A late trade for a bar already closed. Dropped rather than
            # reopening the bar: a structure that can change after the
            # fact is a structure no downstream reading can be replayed
            # against.
            return None

        if self._open is None or bucket != self._bucket:
            self._bucket = bucket
            self._open = {"open": trade.price, "high": trade.price,
                          "low": trade.price, "close": trade.price,
                          "volume": trade.quantity}
        else:
            self._open["high"] = max(self._open["high"], trade.price)
            self._open["low"] = min(self._open["low"], trade.price)
            self._open["close"] = trade.price
            self._open["volume"] += trade.quantity
        return closed

    def _finish(self) -> Optional[Candle]:
        if self._open is None or self._bucket is None:
            return None
        candle = Candle(start_ms=self._bucket, open=self._open["open"],
                        high=self._open["high"], low=self._open["low"],
                        close=self._open["close"], volume=self._open["volume"],
                        closed=True)
        self.candles.append(candle)
        if len(self.candles) > MAX_CANDLES:
            self.candles.pop(0)
        self._open = None
        return candle

    def forming(self) -> Optional[Candle]:
        """The bar in progress, marked NOT closed so every structure
        routine here skips it."""
        if self._open is None or self._bucket is None:
            return None
        return Candle(start_ms=self._bucket, open=self._open["open"],
                      high=self._open["high"], low=self._open["low"],
                      close=self._open["close"], volume=self._open["volume"],
                      closed=False)

    def series(self) -> List[Candle]:
        forming = self.forming()
        return self.candles + ([forming] if forming else [])

    def closed_count(self) -> int:
        return len(self.candles)
