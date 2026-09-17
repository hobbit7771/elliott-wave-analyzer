"""The Lead Engine's own structure read. Small, deterministic, isolated.

The project already has a market-structure module. This does not call it
and does not extend it, for the reason the specification gives: wiring a
realtime microstructure engine into the analyser's structure code makes
one thing out of two, and then a change to either is a change to both.
What is here is smaller than that module on purpose - it answers only the
questions the pressure and pre-break scores actually ask:

  HH / HL / LH / LL   the last swing's relationship to the one before.
  BOS                 a swing high taken out in an uptrend, or a swing low
                      in a downtrend. Continuation.
  CHoCH               the FIRST break against the prevailing structure.
                      The change-of-character, and the reason to care.
  sweep               a wick through a prior extreme that closes back
                      inside it. Liquidity taken, direction not confirmed.
  FVG                 a three-candle gap where the outer candles do not
                      overlap.
  OB                  the last opposite-direction candle before the move
                      that broke structure.
  premium/discount    where price sits inside the current swing range.

Swings come from a fractal test over closed candles only. "Closed only"
is the no-lookahead guarantee at this level: a swing high needs candles
on BOTH sides of it, so the newest bar can never be a swing, and no
feature here can be influenced by a bar that had not finished when the
feature was computed.
"""

from __future__ import annotations

from bisect import bisect_left

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from t3_engine.lead_engine.rolling import clamp

# Bars either side of a candidate for it to count as a swing. Two is the
# usual fractal; it costs two bars of delay and removes most of the noise.
FRACTAL_WIDTH = 2

# How many candles the structure read looks back over.
MAX_CANDLES = 400

BULLISH = "bullish"
BEARISH = "bearish"
RANGING = "ranging"


@dataclass(frozen=True)
class Candle:
    """The engine's own candle. Deliberately not the project's Candle
    type: importing that would make this module depend on the analyser's
    model layer, which is exactly the coupling being avoided."""

    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = True


@dataclass(frozen=True)
class Swing:
    index: int
    timestamp_ms: int
    price: float
    kind: str           # "high" | "low"


@dataclass
class FairValueGap:
    direction: str
    top: float
    bottom: float
    timestamp_ms: int

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass
class SmcState:
    symbol: str
    interval: str
    trend: str = RANGING
    last_swing_label: str = ""
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    bos: bool = False
    bos_direction: str = ""
    choch: bool = False
    choch_direction: str = ""
    sweep: str = ""
    order_block: Optional[Dict[str, float]] = None
    fair_value_gaps: List[Dict[str, float]] = field(default_factory=list)
    premium_discount: str = "unknown"
    range_position: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "interval": self.interval, "trend": self.trend,
            "last_swing": self.last_swing_label,
            "swing_high": self.swing_high, "swing_low": self.swing_low,
            "bos": self.bos, "bos_direction": self.bos_direction,
            "choch": self.choch, "choch_direction": self.choch_direction,
            "sweep": self.sweep, "order_block": self.order_block,
            "fair_value_gaps": self.fair_value_gaps[:5],
            "premium_discount": self.premium_discount,
            "range_position": self.range_position,
        }


def find_swings(candles: List[Candle], width: int = FRACTAL_WIDTH) -> List[Swing]:
    """Fractal swings over CLOSED candles only.

    A candidate needs `width` bars on each side, so the last `width` bars
    can never produce a swing. That delay is the no-lookahead property,
    not a limitation to be tuned away.

    TIES ARE ALLOWED ON THE LEFT and not on the right. The first version
    demanded the candidate be strictly beyond every neighbour on both
    sides, and the consequence is worth stating plainly: a DOUBLE BOTTOM
    never confirmed. Two tests of the same support to the tick - the most
    watched support pattern there is - cancelled each other out and the
    level did not exist as far as this engine was concerned. Found by
    feeding the tracker a clean zigzag: six swing highs, zero supports.

    Requiring strictness on the right keeps the confirmation honest: the
    swing is only a swing because of what came AFTER it, and that part
    still has to be decisive.

    Highs and lows are tested independently rather than as an if/elif,
    because the first version silently preferred highs whenever a bar
    could be read as either."""
    closed = [c for c in candles if c.closed]
    out: List[Swing] = []
    for i in range(width, len(closed) - width):
        left = closed[i - width:i]
        right = closed[i + 1:i + width + 1]
        candle = closed[i]

        is_high = (all(candle.high >= c.high for c in left)
                   and all(candle.high > c.high for c in right))
        is_low = (all(candle.low <= c.low for c in left)
                  and all(candle.low < c.low for c in right))
        # A flat window reads as both, and neither reading means anything.
        if is_high and is_low:
            continue
        if is_high:
            out.append(Swing(i, candle.start_ms, candle.high, "high"))
        elif is_low:
            out.append(Swing(i, candle.start_ms, candle.low, "low"))
    return out


def label_swings(swings: List[Swing]) -> str:
    """HH / HL / LH / LL for the newest swing, or ""."""
    if len(swings) < 3:
        return ""
    newest = swings[-1]
    same_kind = [s for s in swings[:-1] if s.kind == newest.kind]
    if not same_kind:
        return ""
    previous = same_kind[-1]
    if newest.kind == "high":
        return "HH" if newest.price > previous.price else "LH"
    return "HL" if newest.price > previous.price else "LL"


def find_fair_value_gaps(candles: List[Candle], limit: int = 5) -> List[FairValueGap]:
    """Three-candle gaps, newest first.

    Bullish when candle i-1's high is below candle i+1's low: price jumped
    and left an untraded band behind it."""
    closed = [c for c in candles if c.closed]
    out: List[FairValueGap] = []
    for i in range(len(closed) - 2, 0, -1):
        before, after = closed[i - 1], closed[i + 1]
        if after.low > before.high:
            out.append(FairValueGap("bullish", after.low, before.high, closed[i].start_ms))
        elif after.high < before.low:
            out.append(FairValueGap("bearish", before.low, after.high, closed[i].start_ms))
        if len(out) >= limit:
            break
    return out


def find_order_block(candles: List[Candle], direction: str) -> Optional[Dict[str, float]]:
    """The last opposite-colour candle before the impulse that broke out.

    Walks back from the newest closed candle for the first candle whose
    body opposes `direction` - the standard construction, and the only one
    this engine needs."""
    closed = [c for c in candles if c.closed]
    for candle in reversed(closed[:-1]):
        down = candle.close < candle.open
        if (direction == BULLISH and down) or (direction == BEARISH and not down):
            return {"open": candle.open, "high": candle.high, "low": candle.low,
                    "close": candle.close, "timestamp_ms": float(candle.start_ms)}
    return None


def _supersedes(incoming: Candle, existing: Candle) -> bool:
    """May `incoming` replace `existing` for the same bar?

    Only one case is forbidden, and it is the one that actually happened:
    a bar that is CLOSED is final, so a still-forming update for the same
    minute is stale news and is discarded. Everything else replaces - a
    close arriving over a forming bar, a forming update over an older
    forming one, a re-delivered closed bar over the identical closed bar
    (a no-op that keeps a repeated backfill idempotent)."""
    return not (existing.closed and not incoming.closed)


class SmcEngine:
    """Structure for one symbol at one interval."""

    def __init__(self, symbol: str, interval: str) -> None:
        self.symbol = symbol.upper()
        self.interval = interval
        self.candles: List[Candle] = []
        self._trend = RANGING
        # The memoised state and the bar it belongs to. See `state()`:
        # the computation advances `_trend`, so without this a second
        # read of the same bar turns CHoCH into BOS.
        self._state_key: Optional[Tuple[int, int]] = None
        self._state: Optional[SmcState] = None
        self._last_bos_level: Optional[float] = None

    def update(self, candle: Candle) -> None:
        """Place a candle in the series, in TIME ORDER.

        Bybit republishes the forming candle on every tick with the same
        `start`, so appending blindly would fill the series with hundreds
        of copies of one bar and destroy every swing calculation. The two
        fast paths - same bar as the newest, or strictly newer - are the
        two the socket takes, and they stay O(1).

        THE THIRD PATH IS WHY THIS IS NOT JUST AN APPEND. An older bar
        used to be appended at the end, leaving the series non-monotonic,
        and `find_swings` is purely positional: it compares each bar with
        its NEIGHBOURS BY INDEX and never looks at a timestamp. A series
        out of order therefore yields a different swing set, different
        levels, and a different break score from the same bars.

        That is not hypothetical. The REST backfill seeds 240 historical
        minutes on its own thread while the kline socket is already
        running, so on any warm start - and deterministically on the
        `subscribe()` path, where the stream never stopped - the live bars
        arrive first and the history lands behind them. Measured on a
        240-bar series with twelve warm minutes: the same bars gave
        nearest support 98.27 in order and 98.13 out of order.

        AND A CLOSED BAR IS FINAL. Replacing was unconditional, so a
        forming update for a minute the backfill had already delivered
        CLOSED overwrote the finished bar with a partial one - the true
        high and low of that minute replaced by whatever had printed in
        the fraction of it the live socket had seen. `_supersedes` is the
        rule that stops it, and it is what makes a warm start, a
        live-before-history start and a repeated backfill all converge on
        the same series instead of three different ones."""
        if self.candles and self.candles[-1].start_ms == candle.start_ms:
            if _supersedes(candle, self.candles[-1]):
                self.candles[-1] = candle
        elif not self.candles or candle.start_ms > self.candles[-1].start_ms:
            self.candles.append(candle)
        else:
            # Out of order: find where it belongs, replacing the bar
            # already at that moment rather than duplicating it.
            index = bisect_left([c.start_ms for c in self.candles],
                                candle.start_ms)
            if index < len(self.candles) and \
                    self.candles[index].start_ms == candle.start_ms:
                if _supersedes(candle, self.candles[index]):
                    self.candles[index] = candle
            else:
                self.candles.insert(index, candle)
        if len(self.candles) > MAX_CANDLES:
            self.candles = self.candles[-MAX_CANDLES:]
        # A trimmed series changes how many closed bars there are, which
        # is half the memo key - but say it out loud rather than relying
        # on that.
        self._state_key = None

    def state(self) -> SmcState:
        """The structure, as of the newest CLOSED bar.

        MEMOISED, and that is a correctness fix rather than a speed one.
        The computation advances `self._trend`, and `state()` is called
        several times per snapshot - by the structure layer, by the frame
        assembler, by the multi-timeframe block. So the FIRST caller saw a
        break against the prevailing trend and got CHoCH, which committed
        the new trend; every caller after it saw the same break WITH the
        now-current trend and got BOS. The same bar, the same data, a
        different answer depending on who asked first.

        A change of character is the single most consequential label this
        module produces - it is what `pressure_component` weighs most
        heavily - so it cannot depend on call order.

        The key is the newest closed bar and how many closed bars there
        are, so the answer changes exactly when a bar closes and not
        before. The forming bar deliberately does not invalidate it:
        everything here is defined over closed bars, and letting a tick
        move BOS/CHoCH would be the lookahead this module exists to
        avoid."""
        closed = [c for c in self.candles if c.closed]
        key = (closed[-1].start_ms if closed else 0, len(closed))
        if self._state_key == key and self._state is not None:
            return self._state
        out = self._compute_state(closed)
        self._state_key = key
        self._state = out
        return out

    def _compute_state(self, closed: List[Candle]) -> SmcState:
        out = SmcState(symbol=self.symbol, interval=self.interval)
        if len(closed) < FRACTAL_WIDTH * 2 + 3:
            return out

        swings = find_swings(closed)
        highs = [s for s in swings if s.kind == "high"]
        lows = [s for s in swings if s.kind == "low"]
        out.swing_high = highs[-1].price if highs else None
        out.swing_low = lows[-1].price if lows else None
        out.last_swing_label = label_swings(swings)

        # Trend from the last two swings of each kind: rising highs AND
        # rising lows is an uptrend, and anything else is not one.
        if len(highs) >= 2 and len(lows) >= 2:
            higher_highs = highs[-1].price > highs[-2].price
            higher_lows = lows[-1].price > lows[-2].price
            if higher_highs and higher_lows:
                new_trend = BULLISH
            elif not higher_highs and not higher_lows:
                new_trend = BEARISH
            else:
                new_trend = RANGING
        else:
            new_trend = RANGING

        last_close = closed[-1].close
        # BOS / CHoCH. A break in the direction of the trend continues it;
        # the first break against it is the change of character, and the
        # distinction is the whole reason both names exist.
        if out.swing_high is not None and last_close > out.swing_high:
            if self._trend == BEARISH:
                out.choch, out.choch_direction = True, BULLISH
            else:
                out.bos, out.bos_direction = True, BULLISH
            new_trend = BULLISH
        elif out.swing_low is not None and last_close < out.swing_low:
            if self._trend == BULLISH:
                out.choch, out.choch_direction = True, BEARISH
            else:
                out.bos, out.bos_direction = True, BEARISH
            new_trend = BEARISH

        # Sweep: the wick went through a prior extreme and the close came
        # back. Liquidity taken without the level actually giving way.
        newest = closed[-1]
        if out.swing_high is not None and newest.high > out.swing_high >= newest.close:
            out.sweep = "high"
        elif out.swing_low is not None and newest.low < out.swing_low <= newest.close:
            out.sweep = "low"

        self._trend = new_trend
        out.trend = new_trend
        out.fair_value_gaps = [
            {"direction": g.direction, "top": g.top, "bottom": g.bottom,
             "timestamp_ms": float(g.timestamp_ms)}
            for g in find_fair_value_gaps(closed)
        ]
        out.order_block = find_order_block(closed, new_trend)

        if out.swing_high is not None and out.swing_low is not None and \
                out.swing_high > out.swing_low:
            span = out.swing_high - out.swing_low
            position = (last_close - out.swing_low) / span
            out.range_position = round(position, 4)
            out.premium_discount = "premium" if position > 0.5 else "discount"
        return out

    def pressure_component(self) -> float:
        """-1..+1 from structure alone.

        A change of character outweighs a break of structure: a
        continuation of a trend that is already visible is worth less than
        the first sign it is over."""
        state = self.state()
        score = 0.0
        if state.trend == BULLISH:
            score += 0.4
        elif state.trend == BEARISH:
            score -= 0.4
        if state.choch:
            score += 0.6 if state.choch_direction == BULLISH else -0.6
        elif state.bos:
            score += 0.3 if state.bos_direction == BULLISH else -0.3
        if state.sweep == "low":
            score += 0.2          # lows swept and reclaimed: buyers defended
        elif state.sweep == "high":
            score -= 0.2
        return clamp(score)


def candle_from_kline(item, interval: str) -> Optional[Candle]:
    """One Bybit kline entry into this module's Candle.

    Bybit sends `confirm` - true once the bar has closed. It is carried
    through rather than ignored because every swing here is computed over
    closed bars only."""
    try:
        return Candle(
            start_ms=int(item.get("start") or item.get("t") or 0),
            open=float(item.get("open") or item.get("o") or 0.0),
            high=float(item.get("high") or item.get("h") or 0.0),
            low=float(item.get("low") or item.get("l") or 0.0),
            close=float(item.get("close") or item.get("c") or 0.0),
            volume=float(item.get("volume") or item.get("v") or 0.0),
            closed=bool(item.get("confirm", True)),
        )
    except (TypeError, ValueError, AttributeError):
        return None
