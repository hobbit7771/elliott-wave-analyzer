# Pressure score

Nine components, each a number on −1…+1 where positive favours the upside,
combined by published weights into **two independent scores**.

## The weights

| Component | Weight | Source |
|---|---|---|
| Order book imbalance | 18% | `OrderBook.pressure_component` (0.6 × weighted OBI + 0.4 × OBI5) |
| Microprice | 10% | drift 2/3, level 1/3 |
| CVD / aggressive flow | 18% | 60s CVD slope, self-scaled by its own range |
| Trade velocity | 10% | z-score magnitude, **signed by the flow it accompanies** |
| Liquidity pulling/replenishment | 10% | netted bid vs ask, as a share |
| Liquidations | 12% | flush state, blended 70/30 with open interest |
| BTC lead-lag | 8% | impulse × correlation × lead weight |
| SMC | 8% | trend, BOS/CHoCH, sweep |
| Elliott context | 6% | wave candidate |

They sum to 1.0 (asserted by a test) and live in `config.PressureWeights`.
Override one at a time: `T3_LEAD_ENGINE_WEIGHT_CVD=0.25`.

Velocity has no direction of its own, so it is signed by the flow it
accompanies. Fast trading in a balanced market is noise; fast trading that
is overwhelmingly one-sided is the event.

## Two scores, not one and its complement

```
long  = 100 × Σ wᵢ · max(0, cᵢ) / Σ wᵢ
short = 100 × Σ wᵢ · max(0, −cᵢ) / Σ wᵢ
```

`long = 100 − short` would mean a featureless market reads as "50 long",
which sounds like a position. Here:

* nothing happening → **low on both**
* one-sided market → **high on one, low on the other**
* a market being fought over → **high on both**, which is a real and
  distinct state and one worth seeing before taking a trade either way

`conflict = min(long, short)` reports exactly that, and the signal machine
refuses a directional state through it.

## Missing components are dropped, not zeroed

A component whose stream has not produced enough to answer returns `None`.
Its weight is **redistributed** over the rest and its name appears in
`missing`. Counting it as a neutral zero would dilute a genuine reading
toward the middle and make a half-connected engine look calm rather than
uninformed.

## Attribution

Every response carries `contributions` (weight × value per component) and
a one-line `explanation` naming the top three. "82 long" backed entirely
by a heuristic carrying 6% of the weight is a different claim from "82
long" backed by the book and the flow, and a score with no attribution
cannot be judged.
