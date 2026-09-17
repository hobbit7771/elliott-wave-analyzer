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

## Adverse selection, measured

The module began by ASSUMING each maker leg gives back half the spread,
and said it would be replaced the moment real fills could measure it.
They now can, from the tape alone: where the mid goes after an aggressive
trade is what a resting order on that side paid.

| instrument | spread median | adverse selection @1s | @5s | @30s |
|---|---|---|---|---|
| INJUSDT | 1.81 bps | **3.61** | **5.40** | **4.52** |
| ATOMUSDT | 6.53 bps | **6.53** | **6.53** | **6.53** |

INJUSDT gives back two to three times its spread. ATOMUSDT gives back
almost exactly its spread. The assumption of one half was optimistic by a
factor of two to six, so the default is now **1.0** — still below what
INJUSDT measured, and the remaining error is in the safe direction: it
understates the cost of resting, so a strategy failing this gate would
fail a stricter one too.

*Limit of the estimator, stated plainly:* it measures the mid move from
just before the trade, so the bid-ask bounce is inside it and part of
that move is mechanical rather than informational. The qualitative
finding — adverse selection is of the order of the spread or larger —
does not depend on separating the two, and that finding is what decides
hypothesis B. Sample: one window per instrument, 928 and 82 trades.

## Break-even gross move, in bps — with MEASURED adverse selection

| spread (bps) | taker/taker | maker/taker | maker/maker |
|---|---|---|---|
| 1.81 — INJUSDT median | 12.81 | 10.21 | 5.81 |
| 4.00 — the fee floor | 15.00 | 13.50 | 8.00 |
| 6.53 — ATOMUSDT median | 17.53 | 17.30 | 10.53 |
| 10.00 | 21.00 | 22.50 | 14.00 |
| 20.00 | 31.00 | 37.50 | 24.00 |

**The sign of the maker/maker column has flipped, and that is the**
**finding.** Under the assumed half-a-spread it was FLAT at 4bps at
every spread. Under the measured figure it RISES: 5.81bps at
INJUSDT's spread, 10.53 at ATOMUSDT's, 24.00 at twenty.

A wider spread earns more and gives back MORE than it earns. That is
the opposite of the reason one would go looking for a wide
instrument, and it is measured rather than argued.

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
