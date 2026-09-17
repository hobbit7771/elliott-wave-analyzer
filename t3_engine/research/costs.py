"""What a round trip actually costs, per execution regime.

THE 13 BPS FIGURE WAS AN ASSUMPTION, NOT A CONSTANT. The previous
calibration priced every trade at 2 x 5.5bps taker + 2 x 1bp slippage
and concluded that no signal could pay it. That arithmetic is correct
only for taker in and taker out. A strategy that rests on the bid and
leaves on the ask pays 2 x 2bps and CAPTURES the spread rather than
paying it - a different number by a factor of three, and a different
answer to "is there an edge here".

So costs are modelled per regime, and the regime is a property of the
strategy rather than of the venue.

  taker/taker  enter and exit by crossing. Pays the spread twice, pays
               taker fees twice, and pays whatever depth the size eats.
  maker/taker  rest to enter, cross to exit. The common shape: patient
               entry, decisive exit.
  maker/maker  rest on both sides. Cheapest in fees and it earns the
               spread - and it is the only regime where the fill itself
               is uncertain, so its cost model has to carry that
               uncertainty rather than assume the fill.

NO REBATE IS ASSUMED. Bybit's maker fee for a non-VIP linear account is
a positive 0.02%, not a rebate. Assuming a negative maker fee is the
single easiest way to manufacture a profitable market-making backtest,
so the default here is the published retail schedule and `assumed=True`
says out loud that nobody verified it against a real account.

FUNDING IS SEPARATE AND CONDITIONAL. It is charged every eight hours to
whoever holds the position at the stamp. A trade that is flat at the
stamp pays none of it, so folding an average funding rate into a
per-trade cost overstates the bill for short holds and understates it
for long ones. `funding_cost_bps` therefore takes the hold interval and
the stamps it crossed.

WHAT MUST NOT BE DOUBLE COUNTED. When a fill price comes out of the
simulator it ALREADY contains the spread and the depth walked. Adding a
"slippage" term on top of such a fill charges the same cost twice. The
`CostModel.fees_only` path exists for that case and the docstrings say
which is which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

TAKER_TAKER = "taker/taker"
MAKER_TAKER = "maker/taker"
MAKER_MAKER = "maker/maker"
REGIMES = (TAKER_TAKER, MAKER_TAKER, MAKER_MAKER)

MAKER = "maker"
TAKER = "taker"

# Bybit USDT-perpetual (linear), non-VIP, published retail schedule.
# Percentages of notional, expressed here in basis points.
#   https://bybit-exchange.github.io/docs/v5/account/fee-rate
# These are DEFAULTS, and `FeeSchedule.assumed` marks that no account was
# queried to confirm them. A real account can differ (VIP tiers, promos),
# which is why `sensitivity()` exists.
BYBIT_LINEAR_TAKER_BPS = 5.5
BYBIT_LINEAR_MAKER_BPS = 2.0

# Funding is charged every eight hours, at 00:00, 08:00 and 16:00 UTC.
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000

# How much of the spread a resting order gives back, per maker leg, when
# nobody has measured it. One half is the uninformed-maker result: a
# quote is lifted exactly when someone wanted to trade against it, and on
# average that costs the provider the edge the spread was meant to earn.
# An assumption, varied in the stress scenarios and replaced the moment
# real fills can measure it.
DEFAULT_ADVERSE_SELECTION_SHARE = 0.5


@dataclass(frozen=True)
class FeeSchedule:
    maker_bps: float = BYBIT_LINEAR_MAKER_BPS
    taker_bps: float = BYBIT_LINEAR_TAKER_BPS
    assumed: bool = True
    source: str = "Bybit published non-VIP linear schedule; no account queried"

    def side_bps(self, liquidity: str) -> float:
        return self.maker_bps if liquidity == MAKER else self.taker_bps

    def round_trip_bps(self, regime: str) -> float:
        if regime == TAKER_TAKER:
            return 2 * self.taker_bps
        if regime == MAKER_TAKER:
            return self.maker_bps + self.taker_bps
        if regime == MAKER_MAKER:
            return 2 * self.maker_bps
        raise ValueError(f"unknown regime {regime!r}")


@dataclass
class CostLine:
    """One row of the waterfall: what it is, what it cost, why."""
    name: str
    bps: float
    note: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "bps": round(self.bps, 3), "note": self.note}


@dataclass
class CostWaterfall:
    regime: str
    gross_bps: float
    lines: List[CostLine] = field(default_factory=list)

    @property
    def total_cost_bps(self) -> float:
        return sum(line.bps for line in self.lines)

    @property
    def net_bps(self) -> float:
        return self.gross_bps - self.total_cost_bps

    def as_dict(self) -> Dict[str, object]:
        return {
            "regime": self.regime,
            "gross_bps": round(self.gross_bps, 3),
            "lines": [line.as_dict() for line in self.lines],
            "total_cost_bps": round(self.total_cost_bps, 3),
            "net_bps": round(self.net_bps, 3),
        }

    def render(self) -> str:
        width = max(len(line.name) for line in self.lines) if self.lines else 10
        out = [f"{self.regime}", f"  {'gross move':<{width}}  {self.gross_bps:+8.2f} bps"]
        for line in self.lines:
            out.append(f"  {line.name:<{width}}  {-line.bps:+8.2f} bps"
                       + (f"   ({line.note})" if line.note else ""))
        out.append(f"  {'NET':<{width}}  {self.net_bps:+8.2f} bps")
        return "\n".join(out)


def funding_cost_bps(rate_per_interval: float, hold_ms: int,
                     entered_at_ms: int, side: str = "long") -> float:
    """Funding actually charged over one hold, in bps of notional.

    Counts the STAMPS CROSSED rather than prorating: a position opened at
    07:59 and closed at 08:01 pays a full interval; one held from 08:01
    to 15:59 pays nothing. A longs-pay-shorts positive rate costs a long
    and pays a short, which is why `side` is here rather than assumed."""
    if hold_ms <= 0:
        return 0.0
    first = (entered_at_ms // FUNDING_INTERVAL_MS + 1) * FUNDING_INTERVAL_MS
    crossed = 0
    stamp = first
    while stamp <= entered_at_ms + hold_ms:
        crossed += 1
        stamp += FUNDING_INTERVAL_MS
    signed = rate_per_interval if side == "long" else -rate_per_interval
    return crossed * signed * 10_000.0


class CostModel:
    def __init__(self, fees: Optional[FeeSchedule] = None) -> None:
        self.fees = fees or FeeSchedule()

    # ---- the two ways to charge ---------------------------------------

    def fees_only_bps(self, entry_liquidity: str, exit_liquidity: str) -> float:
        """For a P&L built from SIMULATED FILL PRICES.

        Those prices already contain the spread and the depth the order
        walked, so the only thing still owed is the exchange's cut.
        Charging spread again here is the double count the module
        docstring warns about."""
        return (self.fees.side_bps(entry_liquidity)
                + self.fees.side_bps(exit_liquidity))

    def modelled_round_trip_bps(self, regime: str, spread_bps: float,
                                extra_slippage_bps: float = 0.0,
                                adverse_selection_bps: float = 0.0) -> CostWaterfall:
        """For an ANALYTIC estimate, where no simulator produced a fill.

        Used to answer "what would this have to earn to be worth doing"
        before any strategy exists. The spread term differs by regime and
        that difference is the whole point:

          taker/taker  crosses twice: pays a full spread
          maker/taker  rests once, crosses once: pays half
          maker/maker  rests twice: EARNS the spread, so the term is
                       negative - and adverse selection is the reason
                       that is not free money, so it is charged in full.
        """
        lines: List[CostLine] = []
        fee = self.fees.round_trip_bps(regime)
        lines.append(CostLine("exchange fees", fee,
                              f"{regime} at maker {self.fees.maker_bps}bps / "
                              f"taker {self.fees.taker_bps}bps"))

        if regime == TAKER_TAKER:
            lines.append(CostLine("spread crossed", spread_bps,
                                  "half a spread in, half a spread out"))
        elif regime == MAKER_TAKER:
            lines.append(CostLine("spread crossed", spread_bps / 2.0,
                                  "rested in, crossed out"))
        else:
            lines.append(CostLine("spread earned", -spread_bps,
                                  "both sides rested - this is the revenue, "
                                  "not a cost"))

        if extra_slippage_bps:
            lines.append(CostLine("depth beyond the top", extra_slippage_bps,
                                  "size larger than the best level"))
        if adverse_selection_bps:
            lines.append(CostLine("adverse selection", adverse_selection_bps,
                                  "the market moved against the fill"))
        return CostWaterfall(regime=regime, gross_bps=0.0, lines=lines)

    def break_even_bps(self, regime: str, spread_bps: float,
                       extra_slippage_bps: float = 0.0,
                       adverse_selection_bps: Optional[float] = None) -> float:
        """The gross move, in bps, at which a trade stops losing money.

        A MAKER'S BREAK-EVEN WITHOUT AN ADVERSE-SELECTION TERM IS NOT A
        BREAK-EVEN, and leaving it at zero produces a genuinely absurd
        answer: at a spread of exactly two maker fees, maker/maker
        break-even computes to 0.00bps and every positive expectation,
        however tiny, "pays for itself". It does not. A resting order is
        filled precisely when someone wanted to trade against it, which
        is not a random moment.

        So when the caller does not supply a measured figure, each maker
        leg is charged `DEFAULT_ADVERSE_SELECTION_SHARE` of the spread.
        At the default of one half that is the classic uninformed-maker
        result - a market maker adversely selected by half the spread
        earns nothing from the spread, leaving the fees as the whole cost
        - and it is an ASSUMPTION, which is why the stress scenarios vary
        it and why measuring it from fills replaces it."""
        if adverse_selection_bps is None:
            adverse_selection_bps = (self.maker_legs(regime) * spread_bps
                                     * DEFAULT_ADVERSE_SELECTION_SHARE)
        return self.modelled_round_trip_bps(
            regime, spread_bps, extra_slippage_bps,
            adverse_selection_bps).total_cost_bps

    @staticmethod
    def maker_legs(regime: str) -> int:
        return {TAKER_TAKER: 0, MAKER_TAKER: 1, MAKER_MAKER: 2}[regime]

    # ---- sensitivity ---------------------------------------------------

    def sensitivity(self, regime: str, spreads_bps: Sequence[float],
                    adverse_bps: Sequence[float] = (0.0,)) -> List[Dict[str, float]]:
        """Break-even across a grid, because one number pretending to be
        the cost is how a marginal strategy gets approved."""
        rows = []
        for spread in spreads_bps:
            for adverse in adverse_bps:
                rows.append({
                    "regime": regime,
                    "spread_bps": spread,
                    "adverse_selection_bps": adverse,
                    "break_even_bps": round(
                        self.break_even_bps(regime, spread,
                                            adverse_selection_bps=adverse), 3),
                })
        return rows


def compare_regimes(spread_bps: float, fees: Optional[FeeSchedule] = None,
                    adverse_selection_bps: float = 0.0) -> List[Dict[str, object]]:
    """One table: what each regime needs to earn at a given spread."""
    model = CostModel(fees)
    out = []
    for regime in REGIMES:
        waterfall = model.modelled_round_trip_bps(
            regime, spread_bps, adverse_selection_bps=adverse_selection_bps)
        out.append({
            "regime": regime,
            "spread_bps": spread_bps,
            "fees_bps": round(model.fees.round_trip_bps(regime), 3),
            "break_even_gross_bps": round(waterfall.total_cost_bps, 3),
            "waterfall": waterfall.as_dict(),
        })
    return out
