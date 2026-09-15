"""Aggressive flow: who is crossing the spread, how hard, how fast.

Bybit's `publicTrade` carries the TAKER's side in `S` - a "Buy" is
someone lifting the offer. That single field is what separates this from
volume: 1,000 contracts traded says nothing, 1,000 contracts traded by
buyers lifting offers into a thinning book says a great deal.

Everything is a rolling window keyed on the exchange timestamp (see
rolling.py for why arrival time is not usable). The windows are the ones
the specification names, 250ms through 5 minutes, and they all read the
same underlying series rather than each keeping a copy.

Velocity gets a z-score, not a fixed threshold, because "fast" is not a
constant: forty trades a second is dead for BTCUSDT and a stampede for
FILUSDT. The z-score is taken against this instrument's own recent
history, so the comparison is always like for like.

A "large" trade is likewise relative - a multiple of this instrument's
own median trade size. See config.Thresholds.large_trade_multiple.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from t3_engine.lead_engine.config import Thresholds
from t3_engine.lead_engine.normalize import normalized_delta
from t3_engine.lead_engine.rolling import (
    TimeSeries,
    WINDOWS_MS,
    WINDOW_LABELS,
    clamp,
    median,
    scale_to_unit,
    zscore,
)

EPSILON = 1e-9

# Where the raw diagnostic ratio is truncated for display. It has no
# effect on any score - nothing scores on the ratio - and it exists so
# a one-sided 250ms window prints "999" rather than a seven-figure
# number that reads like a bug.
RATIO_DISPLAY_CAP = 999.0

# How many one-second velocity readings the z-score is measured against.
VELOCITY_HISTORY = 120


@dataclass(frozen=True)
class Trade:
    timestamp_ms: int
    price: float
    quantity: float
    is_taker_buy: bool

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass
class WindowFlow:
    label: str
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trades: int = 0
    notional: float = 0.0

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def delta_ratio(self) -> float:
        """Buy volume over sell volume. A DIAGNOSTIC, not a feature.

        Unbounded by construction: a 250ms window with no sellers sends it
        to the guard value, and the old scorer fed exactly this into a
        weighted sum, which is how a "delta ratio" of a million ended up
        driving a signal. Nothing scores on this any more - see
        `normalized_delta` - and it is capped where it is reported so a
        panel cannot print a number no one can read."""
        return self.buy_volume / max(self.sell_volume, EPSILON)

    @property
    def normalized_delta(self) -> float:
        """(buy - sell) / (buy + sell). Always in [-1, +1]."""
        return normalized_delta(self.buy_volume, self.sell_volume)

    def as_dict(self) -> Dict[str, float]:
        return {
            "buy_volume": round(self.buy_volume, 8),
            "sell_volume": round(self.sell_volume, 8),
            "delta": round(self.delta, 8),
            # The bounded form is what anything downstream should read.
            "normalized_delta": round(self.normalized_delta, 6),
            "delta_ratio": round(min(self.delta_ratio, RATIO_DISPLAY_CAP), 6),
            "trades": self.trades,
            "notional": round(self.notional, 4),
        }


class TradeFlow:
    def __init__(self, symbol: str, thresholds: Optional[Thresholds] = None) -> None:
        self.symbol = symbol.upper()
        self.thresholds = thresholds or Thresholds()
        self.trades: TimeSeries = TimeSeries(horizon_ms=max(WINDOWS_MS))
        self._velocity_history: List[float] = []
        self._last_velocity_bucket: Optional[int] = None
        self.last_price: Optional[float] = None

    # ---- ingest ----

    def add(self, trade: Trade) -> None:
        self.trades.add(trade.timestamp_ms, trade)
        self.last_price = trade.price
        self._roll_velocity(trade.timestamp_ms)

    def _roll_velocity(self, timestamp_ms: int) -> None:
        """Bank one reading per whole second, so the z-score compares
        like with like. Sampling per trade instead would make the history
        denser exactly when trading is fast, which is the same bias the
        z-score is there to remove."""
        bucket = timestamp_ms // 1000
        if self._last_velocity_bucket is None:
            self._last_velocity_bucket = bucket
            return
        if bucket == self._last_velocity_bucket:
            return
        previous = self._last_velocity_bucket * 1000
        count = len(self.trades.window(1_000, now_ms=previous + 999))
        self._velocity_history.append(float(count))
        if len(self._velocity_history) > VELOCITY_HISTORY:
            self._velocity_history.pop(0)
        self._last_velocity_bucket = bucket

    # ---- windows ----

    def flow(self, window_ms: int) -> WindowFlow:
        label = WINDOW_LABELS.get(window_ms, f"{window_ms}ms")
        out = WindowFlow(label=label)
        for item in self.trades.window(window_ms):
            trade: Trade = item                 # type: ignore[assignment]
            if trade.is_taker_buy:
                out.buy_volume += trade.quantity
            else:
                out.sell_volume += trade.quantity
            out.trades += 1
            out.notional += trade.notional
        return out

    def all_windows(self) -> Dict[str, Dict[str, float]]:
        return {WINDOW_LABELS[w]: self.flow(w).as_dict() for w in WINDOWS_MS}

    # ---- velocity ----

    def velocity(self, window_ms: int = 1_000) -> Dict[str, float]:
        flow = self.flow(window_ms)
        seconds = max(EPSILON, window_ms / 1000.0)
        return {
            "trades_per_sec": flow.trades / seconds,
            "volume_per_sec": (flow.buy_volume + flow.sell_volume) / seconds,
            "notional_per_sec": flow.notional / seconds,
        }

    def acceleration(self) -> float:
        """Change in trades/sec between the last second and the one before.

        Positive means activity is still building. Computed from the two
        adjacent one-second windows rather than from a longer regression,
        because the point of this number is to be early."""
        newest = self.trades.newest_timestamp()
        if newest is None:
            return 0.0
        current = len(self.trades.window(1_000, now_ms=newest))
        previous = len(self.trades.window(1_000, now_ms=newest - 1_000))
        return float(current - previous)

    def velocity_zscore(self) -> float:
        return zscore(self.velocity(1_000)["trades_per_sec"], self._velocity_history)

    def velocity_state(self) -> str:
        score = self.velocity_zscore()
        if score >= self.thresholds.velocity_zscore_extreme:
            return "extreme"
        if score >= self.thresholds.velocity_zscore_elevated:
            return "elevated"
        return "normal"

    # ---- large prints ----

    def median_trade_size(self) -> float:
        return median(t.quantity for t in self.trades.window(300_000))  # type: ignore[attr-defined]

    def large_trades(self, window_ms: int = 60_000) -> Dict[str, float]:
        reference = self.median_trade_size()
        threshold = reference * self.thresholds.large_trade_multiple
        buys = sells = 0
        buy_volume = sell_volume = 0.0
        if threshold > 0:
            for item in self.trades.window(window_ms):
                trade: Trade = item             # type: ignore[assignment]
                if trade.quantity < threshold:
                    continue
                if trade.is_taker_buy:
                    buys += 1
                    buy_volume += trade.quantity
                else:
                    sells += 1
                    sell_volume += trade.quantity
        return {
            "threshold": round(threshold, 8), "median_trade_size": round(reference, 8),
            "buy_count": buys, "sell_count": sells,
            "buy_volume": round(buy_volume, 8), "sell_volume": round(sell_volume, 8),
        }

    # ---- scores ----

    def pressure_component(self) -> float:
        """Aggression, on -1..+1.

        Built from the 5s delta as a share of that window's total volume,
        which is self-normalising: it answers "what fraction of what
        traded was buyers crossing", and that fraction means the same
        thing on every instrument and at every level of activity."""
        flow = self.flow(5_000)
        total = flow.buy_volume + flow.sell_volume
        if total <= 0:
            return 0.0
        return clamp(flow.delta / total)

    def velocity_component(self) -> float:
        """Velocity has no direction of its own, so it is signed by the
        direction of the flow it accompanies. Fast trading in a market
        with balanced flow is noise; fast trading that is overwhelmingly
        one-sided is the event."""
        magnitude = scale_to_unit(max(0.0, self.velocity_zscore()),
                                  self.thresholds.velocity_zscore_extreme)
        return clamp(magnitude * self.pressure_component())

    def as_dict(self) -> Dict[str, object]:
        velocity = self.velocity()
        return {
            "windows": self.all_windows(),
            "trades_per_sec": round(velocity["trades_per_sec"], 4),
            "volume_per_sec": round(velocity["volume_per_sec"], 6),
            "notional_per_sec": round(velocity["notional_per_sec"], 2),
            "acceleration": round(self.acceleration(), 4),
            "velocity_zscore": round(self.velocity_zscore(), 4),
            "velocity_state": self.velocity_state(),
            "large_trades": self.large_trades(),
            "last_price": self.last_price,
        }
