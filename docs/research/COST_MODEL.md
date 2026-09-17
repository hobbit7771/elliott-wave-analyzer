# Cost model, and the break-even it implies

Generated from `t3_engine/research/costs.py`. Every number below is
produced by the code the simulator charges with, so the document and
the model cannot drift apart.

## Fees

* maker: **2.0 bps** of notional, per side
* taker: **5.5 bps** of notional, per side
* source: Bybit published non-VIP linear schedule; no account queried
* assumed rather than queried: **True**

No rebate is assumed anywhere. Bybit's non-VIP linear maker fee is a
POSITIVE 2 bps, not a rebate, and assuming a negative maker fee is the
single easiest way to manufacture a profitable market-making backtest.

## Round-trip fees by regime

| regime | fees (bps) | what it does |
|---|---|---|
| taker/taker | 11.0 | crosses in and out |
| maker/taker | 7.5 | rests in, crosses out |
| maker/maker | 4.0 | rests on both sides |

The previous round of this project priced every trade at 13 bps and
concluded no signal could pay it. That figure is correct for taker in
and taker out at a 2 bps spread, **and for nothing else**. It is not a
property of the venue, it is a property of one execution choice.

## Adverse selection is not optional

A maker's break-even without an adverse-selection term is not a
break-even. At a spread of exactly two maker fees the modelled
maker/maker cost comes out 0.00 bps, so any positive expectation
'pays for itself' - which is absurd: a resting order is filled
precisely when someone wanted to trade against it, and that is not a
random moment.

So each maker leg is charged **50% of the spread**
unless a measured figure is supplied. That is the classic uninformed-
maker result: a provider adversely selected by half the spread earns
nothing from the spread, leaving the fees as the whole cost. It is an
ASSUMPTION, it is varied in the stress scenarios, and it is replaced
the moment real fills can measure it.

## Break-even gross move, in bps

| spread (bps) | taker/taker | maker/taker | maker/maker |
|---|---|---|---|
| 0.5 | 11.50 | 8.00 | 4.00 |
| 1 | 12.00 | 8.50 | 4.00 |
| 2 | 13.00 | 9.50 | 4.00 |
| 5 | 16.00 | 12.50 | 4.00 |
| 10 | 21.00 | 17.50 | 4.00 |
| 20 | 31.00 | 27.50 | 4.00 |
| 50 | 61.00 | 57.50 | 4.00 |

Two things fall out of this table and both are decisive.

**Crossing scales with the spread; resting does not.** A taker pays the
spread, so a wide market punishes it directly. A maker earns the spread
and gives it back to adverse selection, so its break-even is FLAT.

**maker/maker break-even is 4 bps at every spread.**
That is the falsifiable prediction this model makes before any data:
passive spread capture on this venue at this fee tier needs a gross
edge of 4 bps per round trip that does NOT come from the spread - and
the spread is the only thing the strategy was supposed to earn. On an
instrument whose spread sits near one tick, it cannot work however it
is parameterised. `SpreadCapture.min_spread_bps()` is derived from the
fee schedule for exactly this reason and is not a tunable.

## Worked waterfalls

```
taker/taker
  gross move        +11.70 bps
  exchange fees     -11.00 bps   (taker/taker at maker 2.0bps / taker 5.5bps)
  spread crossed     -2.00 bps   (half a spread in, half a spread out)
  NET                -1.30 bps
```

```
maker/taker
  gross move           +11.70 bps
  exchange fees         -7.50 bps   (maker/taker at maker 2.0bps / taker 5.5bps)
  spread crossed        -1.00 bps   (rested in, crossed out)
  adverse selection     -1.00 bps   (the market moved against the fill)
  NET                   +2.20 bps
```

```
maker/maker
  gross move            +0.00 bps
  exchange fees         -4.00 bps   (maker/maker at maker 2.0bps / taker 5.5bps)
  spread earned         +2.00 bps   (both sides rested - this is the revenue, not a cost)
  adverse selection     -2.00 bps   (the market moved against the fill)
  NET                   -4.00 bps
```

The first is the previous round's signal - a median favourable
excursion of 11.7 bps - priced the way that round priced it. The second
is the same signal resting on entry instead of crossing: still
negative, but by 1.9 bps rather than 3.3. Neither pays.

## Funding

Funding is charged every eight hours to whoever holds the position at
the stamp. `funding_cost_bps` counts the STAMPS CROSSED rather than
prorating: a position opened at 07:59 and closed at 08:01 pays a full
interval, and one held from 08:01 to 15:59 pays nothing. Folding an
average rate into a per-trade cost overstates the bill for short holds
and understates it for long ones.

Every strategy here holds for minutes, so funding is usually zero -
and when a result reports zero funding, that is the reason, not an
omission.

## What must not be double counted

A fill price out of the simulator ALREADY contains the spread and the
depth the order walked. Subtracting a 'slippage' term on top charges
the same cost twice. `Portfolio` therefore subtracts only fees and
funding, and `CostModel.fees_only_bps` exists for that path. The
analytic table above is for the other case: deciding what a strategy
would have to earn before any simulator has produced a fill.
