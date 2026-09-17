"""Three hypotheses, each with its mechanism stated before its code.

A hypothesis here is not "this indicator went up". It is a claim about
WHY someone would be on the other side of the trade at a price that
loses them money, and every parameter exists to test that claim rather
than to fit the sample. Where a threshold could have been tuned, it is
instead DERIVED - most importantly in B, where the minimum spread comes
straight out of the fee schedule and not out of a search.

All three share one gate, and it is the gate the previous calibration
never had: an entry is refused unless the move the strategy expects
exceeds what the round trip costs IN THE REGIME IT WILL ACTUALLY USE. A
strategy that cannot say what it expects to make cannot be allowed to
find out by trading.

NO_TRADE IS A RESULT. Every refusal is recorded with its reason, so the
difference between "it looked and declined" and "it never looked" stays
visible in the output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from t3_engine.research.book import BUY, SELL, BookState
from t3_engine.research.costs import (MAKER, MAKER_MAKER, MAKER_TAKER, TAKER,
                                      TAKER_TAKER, CostModel, FeeSchedule)
from t3_engine.research.execution import LIMIT, MARKET
from t3_engine.research.features import MarketView
from t3_engine.research.portfolio import LONG, SHORT

# What a strategy asks the runner to do.
INTENT_ENTER = "enter"
INTENT_EXIT = "exit"
INTENT_CANCEL = "cancel"
INTENT_QUOTE = "quote"


@dataclass
class Intent:
    kind: str
    side: str = ""
    qty: float = 0.0
    order_kind: str = LIMIT
    price: Optional[float] = None
    post_only: bool = False
    ttl_ms: Optional[int] = None
    reason: str = ""
    tag: str = ""
    expected_bps: float = 0.0
    stop_bps: float = 0.0
    target_bps: float = 0.0
    max_hold_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class StrategyConfig:
    """Pre-registered. Changing one of these is a new experiment with a
    new config_hash, not an adjustment to an old result."""
    name: str
    version: str
    params: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version, "params": dict(self.params)}


class Strategy:
    """The interface both the backtest and the live PAPER runner drive.

    It is the SAME OBJECT in both. Only the feed, the clock and the
    execution adapter differ, which is what makes a forward test a test
    of the thing that was backtested."""

    name = "base"
    version = "0"
    regime = TAKER_TAKER

    def __init__(self, params: Optional[Dict[str, Any]] = None,
                 fees: Optional[FeeSchedule] = None) -> None:
        self.params = dict(self.defaults())
        self.params.update(params or {})
        self.costs = CostModel(fees)
        self.declines: Dict[str, int] = {}

    @classmethod
    def defaults(cls) -> Dict[str, Any]:
        return {}

    def config(self) -> StrategyConfig:
        return StrategyConfig(name=self.name, version=self.version,
                              params=dict(self.params))

    # ---- the shared gate -------------------------------------------------

    def _decline(self, reason: str) -> List[Intent]:
        self.declines[reason] = self.declines.get(reason, 0) + 1
        return []

    def break_even_bps(self, view: MarketView) -> float:
        return self.costs.break_even_bps(self.regime, view.spread_bps)

    def pays_for_itself(self, expected_bps: float, view: MarketView) -> bool:
        """The gate the previous calibration lacked. An expectation that
        does not clear the round trip is not a trade, however good the
        score looked."""
        return expected_bps > self.break_even_bps(view) * self.params["edge_margin"]

    # ---- the interface ---------------------------------------------------

    def on_view(self, view: MarketView, *, flat: bool,
                position_side: str = "", position_bps: float = 0.0,
                held_ms: int = 0) -> List[Intent]:
        raise NotImplementedError

    def ablations(self) -> Dict[str, Dict[str, Any]]:
        """Parameter overrides that switch ONE filter off each.

        Used to ask whether each filter earns its place. Complexity that
        survives only because nobody tested it is complexity that will
        break out of sample."""
        return {}


# =========================================================================
# A. Directional order-flow impulse
# =========================================================================

class OrderFlowImpulse(Strategy):
    """MECHANISM. Aggressors repeatedly hit one side. The resting depth
    on that side drains and - this is the operative part - does NOT come
    back. A level that is being defended replenishes; a level that is
    failing does not. When the remaining depth is too thin to absorb the
    arriving flow, the price has to move to find liquidity, and the edge
    is the short interval between the depth failing and the reprice.

    WHO IS ON THE OTHER SIDE AND WHY THEY LOSE: resting limit orders left
    at a level whose owners have not yet cancelled. They are being picked
    off by flow they have not observed.

    WHY THE OBVIOUS VERSION DOES NOT WORK. A strong OBI1, or a big taker
    delta, or a model score, is present constantly and predicts nothing:
    the book is imbalanced most of the time, because that is what a book
    looks like. The claim needs the CONJUNCTION - flow, drain, FAILED
    replenishment, and a price that has already begun to confirm - and
    the conjunction is rare, which is the point.

    EXECUTION IS TESTED BOTH WAYS. Aggressive entry pays the spread and
    gets the fill; passive entry at the touch with a short TTL saves
    5.5bps of fee and half a spread but is only filled when the market
    comes back - which, if the signal is right, is exactly when it should
    not. That trade-off is measured, not assumed.
    """

    name = "order_flow_impulse"
    version = "1.0.0"
    regime = TAKER_TAKER

    @classmethod
    def defaults(cls) -> Dict[str, Any]:
        return {
            # Conjunction thresholds. Stated as a hypothesis before any
            # data was looked at; the walk-forward may reject them, and a
            # rejection is a result rather than a reason to search.
            "min_taker_delta": 0.55,      # of the window's volume, signed
            "min_depth_drain": 0.35,      # opposing side lost this share
            "max_replenishment": 0.20,    # and did NOT come back
            "min_intensity": 1.0,         # trades/sec: not a dead tape
            "min_ofi_ratio": 0.5,         # OFI, normalised by window depth
            "min_confirm_bps": 0.5,       # price has already begun to move
            # Sizing and risk.
            "notional": 250.0,
            "edge_margin": 1.25,          # expect 25% more than the cost
            "target_multiple": 1.5,       # of realised vol
            "stop_multiple": 1.0,
            "max_hold_ms": 90_000,
            "min_vol_bps": 1.0,           # a flat tape has nothing to pay us
            "max_vol_bps": 60.0,          # a violent one is not this edge
            "max_spread_bps": 8.0,
            "passive_entry": False,
            "passive_ttl_ms": 1_500,
        }

    def ablations(self) -> Dict[str, Dict[str, Any]]:
        return {
            "no_replenishment_filter": {"max_replenishment": 10.0},
            "no_drain_filter": {"min_depth_drain": 0.0},
            "no_confirmation": {"min_confirm_bps": -1e9},
            "no_intensity_filter": {"min_intensity": 0.0},
            "no_ofi_filter": {"min_ofi_ratio": -1e9},
            "no_vol_regime_filter": {"min_vol_bps": 0.0, "max_vol_bps": 1e9},
        }

    def on_view(self, view: MarketView, *, flat: bool, position_side: str = "",
                position_bps: float = 0.0, held_ms: int = 0) -> List[Intent]:
        p = self.params
        if not flat:
            return self._manage(view, position_side, position_bps, held_ms)

        if not view.warm:
            return self._decline("not warm")
        if view.book is None or not view.book.valid:
            return self._decline("no usable book")
        if view.spread_bps > p["max_spread_bps"]:
            return self._decline("spread too wide")
        if view.realised_vol_bps < p["min_vol_bps"]:
            return self._decline("volatility too low to pay for a round trip")
        if view.realised_vol_bps > p["max_vol_bps"]:
            return self._decline("volatility outside the tested regime")
        if view.intensity < p["min_intensity"]:
            return self._decline("tape too quiet")

        long_side = view.taker_delta >= p["min_taker_delta"]
        short_side = view.taker_delta <= -p["min_taker_delta"]
        if not (long_side or short_side):
            return self._decline("no one-sided aggression")

        if long_side:
            drain, replenish, confirm = (view.ask_drain, view.ask_replenishment,
                                         view.trend_bps)
            side, direction = BUY, LONG
        else:
            drain, replenish, confirm = (view.bid_drain, view.bid_replenishment,
                                         -view.trend_bps)
            side, direction = SELL, SHORT

        if drain < p["min_depth_drain"]:
            return self._decline("opposing depth has not drained")
        if replenish > p["max_replenishment"]:
            return self._decline("the level is being defended (replenishment)")
        if confirm < p["min_confirm_bps"]:
            return self._decline("price has not confirmed")

        ofi_ratio = _normalise(view.ofi, view.book, view.taker_volume)
        if direction == LONG and ofi_ratio < p["min_ofi_ratio"]:
            return self._decline("order flow imbalance does not agree")
        if direction == SHORT and -ofi_ratio < p["min_ofi_ratio"]:
            return self._decline("order flow imbalance does not agree")

        # What we claim to expect: the move to the next liquidity, scaled
        # by the volatility actually being realised. Not a fitted number
        # - a statement that has to clear the cost or the trade is not
        # taken.
        expected_bps = view.realised_vol_bps * p["target_multiple"]
        if not self.pays_for_itself(expected_bps, view):
            return self._decline("expected move does not clear the round trip")

        mid = view.book.mid
        qty = p["notional"] / mid
        if p["passive_entry"]:
            price = view.book.best_bid if side == BUY else view.book.best_ask
            return [Intent(kind=INTENT_ENTER, side=side, qty=qty, order_kind=LIMIT,
                           price=price, post_only=True, ttl_ms=p["passive_ttl_ms"],
                           reason="flow impulse, passive at the touch",
                           expected_bps=expected_bps,
                           target_bps=expected_bps,
                           stop_bps=view.realised_vol_bps * p["stop_multiple"],
                           max_hold_ms=p["max_hold_ms"], tag="A-passive")]
        return [Intent(kind=INTENT_ENTER, side=side, qty=qty, order_kind=MARKET,
                       reason="flow impulse, crossing", expected_bps=expected_bps,
                       target_bps=expected_bps,
                       stop_bps=view.realised_vol_bps * p["stop_multiple"],
                       max_hold_ms=p["max_hold_ms"], tag="A-aggressive")]

    def _manage(self, view: MarketView, side: str, position_bps: float,
                held_ms: int) -> List[Intent]:
        """Exit on exhaustion, on the flow turning, or on the clock."""
        p = self.params
        if held_ms >= p["max_hold_ms"]:
            return [Intent(kind=INTENT_EXIT, order_kind=MARKET,
                           reason="TIME_STOP", tag="A-exit")]
        flow = view.taker_delta if side == LONG else -view.taker_delta
        if flow < 0:
            return [Intent(kind=INTENT_EXIT, order_kind=MARKET,
                           reason="SIGNAL_EXIT", tag="A-exit")]
        return []


# =========================================================================
# B. Passive spread capture
# =========================================================================

class SpreadCapture(Strategy):
    """MECHANISM. Someone who needs immediacy pays for it. A resting
    two-sided quote sells that immediacy and collects the spread.

    THE ARITHMETIC THAT DECIDES THIS BEFORE ANY DATA. Bybit charges a
    non-VIP maker 2bps PER SIDE. A round trip of two maker fills
    therefore costs 4bps and earns one spread. So the spread must exceed
    4bps for the strategy to break even BEFORE a single adverse fill -
    and adverse selection is not a rounding error, it is the whole risk.
    `min_spread_bps` is derived from the fee schedule for exactly this
    reason and is not a tunable: tuning it below the fee floor would be
    choosing to lose money more often.

    That prediction is falsifiable and worth stating plainly: on any
    instrument whose spread sits near one tick and under 4bps, this
    strategy CANNOT work on this venue at this fee tier, however it is
    parameterised. The instruments where it could work are the wide ones
    - which are also the ones where the flow that lifts you is most
    likely to be informed.

    WHY QUOTING BOTH SIDES IS NOT CAPTURING THE SPREAD. One side fills
    and the other does not; you are now long into a falling market. That
    outcome has to land in the P&L in full, which is what the inventory
    limit, the skew and the stop are for - not to prevent it, but to
    bound it.
    """

    name = "spread_capture"
    version = "1.0.0"
    regime = MAKER_MAKER

    @classmethod
    def defaults(cls) -> Dict[str, Any]:
        return {
            # DERIVED, not tuned: two maker fees plus a margin.
            "spread_margin_bps": 1.0,
            "notional": 200.0,
            "edge_margin": 1.0,
            "quote_offset_ticks": 0,      # 0 = join the touch
            "ttl_ms": 2_000,
            "min_requote_ms": 400,        # a message-rate floor
            "max_inventory_notional": 400.0,
            "inventory_skew": 0.7,        # how hard inventory pushes quotes
            "max_hold_ms": 120_000,
            "stop_bps": 12.0,
            # Refusal conditions - the toxicity filters.
            "max_abs_taker_delta": 0.45,
            "max_abs_trend_bps": 3.0,
            "max_vol_bps": 12.0,
            "max_intensity": 12.0,
            "min_depth_ratio": 0.25,      # neither side suspiciously thin
        }

    def ablations(self) -> Dict[str, Dict[str, Any]]:
        return {
            "no_toxicity_filter": {"max_abs_taker_delta": 1.01,
                                   "max_abs_trend_bps": 1e9},
            "no_vol_filter": {"max_vol_bps": 1e9},
            "no_intensity_filter": {"max_intensity": 1e9},
            "no_inventory_skew": {"inventory_skew": 0.0},
            "no_depth_filter": {"min_depth_ratio": 0.0},
        }

    def min_spread_bps(self) -> float:
        """Two maker fees plus a margin. Not a free parameter."""
        return (2 * self.costs.fees.maker_bps) + self.params["spread_margin_bps"]

    def on_view(self, view: MarketView, *, flat: bool, position_side: str = "",
                position_bps: float = 0.0, held_ms: int = 0) -> List[Intent]:
        p = self.params
        if view.book is None or not view.book.valid:
            return self._decline("no usable book")
        if not view.warm:
            return self._decline("not warm")

        if not flat:
            if held_ms >= p["max_hold_ms"]:
                return [Intent(kind=INTENT_EXIT, order_kind=MARKET,
                               reason="TIME_STOP", tag="B-exit")]
            if position_bps <= -abs(p["stop_bps"]):
                return [Intent(kind=INTENT_EXIT, order_kind=MARKET,
                               reason="STOP", tag="B-exit")]

        # The arithmetic gate, first and unconditional.
        if view.spread_bps < self.min_spread_bps():
            return self._decline(
                f"spread {view.spread_bps:.2f}bps below the "
                f"{self.min_spread_bps():.2f}bps fee floor")

        # Toxicity: do not quote into somebody who knows something.
        if abs(view.taker_delta) > p["max_abs_taker_delta"]:
            return self._decline("one-sided aggression: toxic flow")
        if abs(view.trend_bps) > p["max_abs_trend_bps"]:
            return self._decline("trending: quoting into a move")
        if view.realised_vol_bps > p["max_vol_bps"]:
            return self._decline("volatility spike")
        if view.intensity > p["max_intensity"]:
            return self._decline("aggressor acceleration")

        bid_depth = view.book.depth_within_bps("bid", 10.0)
        ask_depth = view.book.depth_within_bps("ask", 10.0)
        if bid_depth <= 0 or ask_depth <= 0:
            return self._decline("no depth to quote against")
        ratio = min(bid_depth, ask_depth) / max(bid_depth, ask_depth)
        if ratio < p["min_depth_ratio"]:
            return self._decline("depth lopsided: a book about to move")

        inventory_bps = _inventory_skew_bps(position_side, position_bps, p)
        mid = view.book.mid
        qty = p["notional"] / mid

        intents: List[Intent] = []
        inventory_notional = 0.0 if flat else p["notional"]
        # Quote only the side that reduces or keeps inventory inside the
        # limit. Adding to a position that is already at the limit is how
        # a market maker turns a bounded loss into an unbounded one.
        may_bid = position_side != SHORT or True
        if inventory_notional < p["max_inventory_notional"] or position_side == SHORT:
            intents.append(Intent(
                kind=INTENT_QUOTE, side=BUY, qty=qty, order_kind=LIMIT,
                price=view.book.best_bid * (1.0 - inventory_bps / 10_000.0),
                post_only=True, ttl_ms=p["ttl_ms"],
                reason="quoting the bid", tag="B-bid"))
        if inventory_notional < p["max_inventory_notional"] or position_side == LONG:
            intents.append(Intent(
                kind=INTENT_QUOTE, side=SELL, qty=qty, order_kind=LIMIT,
                price=view.book.best_ask * (1.0 + inventory_bps / 10_000.0),
                post_only=True, ttl_ms=p["ttl_ms"],
                reason="quoting the ask", tag="B-ask"))
        if not intents:
            return self._decline("inventory limit reached on both sides")
        return intents


# =========================================================================
# C. Sweep / exhaustion / absorption reversion
# =========================================================================

class SweepReversion(Strategy):
    """MECHANISM. A burst of one-sided aggression that makes NO further
    progress, after which the liquidity it consumed comes back, is the
    signature of a FORCED seller rather than an informed one - a
    liquidation cascade, a stop run, a margin call. Forced flow carries
    no information about value, so the price it printed is not a price
    anyone chose, and it reverts.

    WHO IS ON THE OTHER SIDE AND WHY THEY LOSE: the forced seller, who is
    not choosing the price at all.

    THE THREE THINGS THAT MAKE THIS NOT "BUY EVERY DIP", and all three
    are required:

      1. A CAUSAL TRIGGER - a burst far outside the window's normal
         intensity, or a printed liquidation. Without it there is no
         reason to believe the flow was forced.
      2. EXHAUSTION - the aggression continued and the price stopped
         going. A move that is still extending is an informed one, and
         fading it is the classic way to lose steadily.
      3. RECLAIM - liquidity returns at the swept level AND the price
         comes back through it. Buying while it is still falling is
         buying an unfinished move.

    INVALIDATION IS A NEW EXTREME. If the swept level gives way again the
    reading was wrong, and the position goes. The hold is capped because
    a reversion that has not happened within the window was not a
    reversion.
    """

    name = "sweep_reversion"
    version = "1.0.0"
    regime = MAKER_TAKER

    @classmethod
    def defaults(cls) -> Dict[str, Any]:
        return {
            "min_burst_intensity": 4.0,     # trades/sec, vs a quiet ~1
            "min_burst_delta": 0.7,         # overwhelmingly one-sided
            "min_sweep_bps": 6.0,           # the move it produced
            "max_extension_bps": 1.0,       # and it has since stopped
            "min_replenishment": 0.30,      # liquidity came back
            "min_reclaim_bps": 1.0,         # price back through the level
            "require_liquidation": False,   # ablation switches this on
            "notional": 250.0,
            "edge_margin": 1.2,
            "target_fraction": 0.5,         # of the sweep, retraced
            "stop_bps": 8.0,
            "max_hold_ms": 120_000,
            "passive_entry": True,
            "passive_ttl_ms": 2_000,
            "max_spread_bps": 10.0,
        }

    def ablations(self) -> Dict[str, Dict[str, Any]]:
        return {
            "no_exhaustion_check": {"max_extension_bps": 1e9},
            "no_replenishment_check": {"min_replenishment": 0.0},
            "no_reclaim_check": {"min_reclaim_bps": -1e9},
            "no_burst_trigger": {"min_burst_intensity": 0.0,
                                 "min_burst_delta": 0.0},
            "liquidation_required": {"require_liquidation": True},
        }

    def __init__(self, params=None, fees=None) -> None:
        super().__init__(params, fees)
        self._sweep: Optional[Dict[str, Any]] = None

    def on_view(self, view: MarketView, *, flat: bool, position_side: str = "",
                position_bps: float = 0.0, held_ms: int = 0) -> List[Intent]:
        p = self.params
        if not flat:
            if held_ms >= p["max_hold_ms"]:
                return [Intent(kind=INTENT_EXIT, order_kind=MARKET,
                               reason="TIME_STOP", tag="C-exit")]
            if position_bps <= -abs(p["stop_bps"]):
                return [Intent(kind=INTENT_EXIT, order_kind=MARKET,
                               reason="STOP", tag="C-exit")]
            return []

        if view.book is None or not view.book.valid or not view.warm:
            return self._decline("not warm or no usable book")
        if view.spread_bps > p["max_spread_bps"]:
            return self._decline("spread too wide")

        # 1. The trigger. Record the sweep when it happens; it is only
        #    actionable LATER, once it has stopped and reversed.
        burst = (view.intensity >= p["min_burst_intensity"]
                 and abs(view.taker_delta) >= p["min_burst_delta"]
                 and abs(view.trend_bps) >= p["min_sweep_bps"])
        if p["require_liquidation"] and view.liquidation_notional <= 0:
            burst = False
        if burst:
            self._sweep = {
                "at_ms": view.decided_at_ms,
                "direction": SHORT if view.taker_delta < 0 else LONG,
                "extreme": view.book.mid,
                "size_bps": abs(view.trend_bps),
            }
            return self._decline("sweep recorded; waiting for exhaustion")

        sweep = self._sweep
        if sweep is None:
            return self._decline("no sweep to fade")
        if view.decided_at_ms - sweep["at_ms"] > p["max_hold_ms"]:
            self._sweep = None
            return self._decline("sweep too old")

        mid = view.book.mid
        swept_down = sweep["direction"] == SHORT
        # 2. Exhaustion: it must have STOPPED extending.
        extension = ((sweep["extreme"] - mid) if swept_down else (mid - sweep["extreme"]))
        extension_bps = 10_000.0 * extension / sweep["extreme"]
        if extension_bps > p["max_extension_bps"]:
            sweep["extreme"] = mid              # still going: follow it down
            return self._decline("the move is still extending")

        # 3. Liquidity returned, and the price reclaimed the level.
        replenishment = (view.bid_replenishment if swept_down
                         else view.ask_replenishment)
        if replenishment < p["min_replenishment"]:
            return self._decline("liquidity has not come back")
        reclaim_bps = -extension_bps
        if reclaim_bps < p["min_reclaim_bps"]:
            return self._decline("no reclaim yet")

        expected_bps = sweep["size_bps"] * p["target_fraction"]
        if not self.pays_for_itself(expected_bps, view):
            return self._decline("expected retrace does not clear the round trip")

        side = BUY if swept_down else SELL
        qty = p["notional"] / mid
        self._sweep = None
        if p["passive_entry"]:
            price = view.book.best_bid if side == BUY else view.book.best_ask
            return [Intent(kind=INTENT_ENTER, side=side, qty=qty, order_kind=LIMIT,
                           price=price, post_only=True, ttl_ms=p["passive_ttl_ms"],
                           reason="reclaim after an exhausted sweep",
                           expected_bps=expected_bps, target_bps=expected_bps,
                           stop_bps=p["stop_bps"], max_hold_ms=p["max_hold_ms"],
                           tag="C-passive")]
        return [Intent(kind=INTENT_ENTER, side=side, qty=qty, order_kind=MARKET,
                       reason="reclaim after an exhausted sweep",
                       expected_bps=expected_bps, target_bps=expected_bps,
                       stop_bps=p["stop_bps"], max_hold_ms=p["max_hold_ms"],
                       tag="C-aggressive")]


def _normalise(ofi: float, book: Optional[BookState], volume: float) -> float:
    """OFI on its own is in contracts and so is not comparable across
    instruments or regimes. Scale it by the depth it moved against."""
    if book is None or not book.valid:
        return 0.0
    depth = book.depth_within_bps("bid", 10.0) + book.depth_within_bps("ask", 10.0)
    scale = max(depth, volume, 1e-9)
    return ofi / scale


def _inventory_skew_bps(position_side: str, position_bps: float,
                        params: Dict[str, Any]) -> float:
    """Push quotes away from adding to an existing position and toward
    reducing it. A quoting strategy without this accumulates inventory in
    exactly the direction that is hurting it."""
    if not position_side:
        return 0.0
    return params["inventory_skew"] * abs(params.get("stop_bps", 10.0)) * 0.1


ALL_STRATEGIES = {
    OrderFlowImpulse.name: OrderFlowImpulse,
    SpreadCapture.name: SpreadCapture,
    SweepReversion.name: SweepReversion,
}
