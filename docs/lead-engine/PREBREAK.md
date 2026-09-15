# Pre-break engine

The module that makes a claim about the next few minutes: *price is
pressed against a level, and the things that normally happen just before
such a level fails are happening now.*

It is **not** a prediction that the break will be profitable, or a
statement about how far price travels afterwards.

## Levels

Derived from fractal swings over **15-second** bars built from the trade
stream (`LEVEL_INTERVAL_MS`). One minute was tried first and was wrong: a
ten-minute capture holds ten bars, too few for a fractal swing to exist at
all, so no level was ever found and the score stayed at zero however clear
the order flow was. Found by replay.

Swings from closed bars only, so a level can never be created by the bar
currently forming — which would let the engine "discover" support at
exactly the price that just printed.

Repeated visits to the same price (within `compression_pct`) increment
`tests`. Departures between visits are recorded as **bounces**.

## The ten features, each 0…1

| Feature | Weight | Reads |
|---|---|---|
| `compression` | 10% | price sitting *on* the level, not visiting it |
| `fading_bounces` | 10% | each departure smaller than the last |
| `depth_drain` | 13% | resting depth on the defending side shrinking over 30s |
| `defender_pulling` | 15% | cancellation share on the defending side |
| `attacker_stacking` | 10% | the other side being reinforced, plus stacked levels |
| `flow_pressure` | 14% | aggressive flow one-sided into the level |
| `microprice_lean` | 8% | the touch leaning through the level |
| `velocity_rising` | 8% | activity accelerating rather than dying out |
| `btc_alignment` | 7% | BTC already going that way |
| `repeated_tests` | 5% | the level hit several times |

The order-flow group (drain, pulling, stacking, flow) carries half the
weight, because those four are the ones that are actually early;
compression and repeated tests describe a setup that may sit there for
hours.

## Why a weighted mean and not a rule cascade

Any one of these alone is a common, meaningless event — depth thins
constantly, bounces shrink constantly. A cascade requiring all ten fires
once a week. The weighted mean lets a strong reading on six of ten carry,
and the full breakdown travels with the number so a probability can always
be taken apart.

## The compression gate

```
probability = weighted_mean × (0.35 + 0.65 × compression)
```

A perfect order-flow reading taken while price is nowhere near the level
is not a pre-break warning **about that level**; it is a description of
the market. Measured: identical flow at the level scores 67, and 26 when
price is 4% away.

## Refusals

* No level of the right kind near price → probability 0, with a note.
* Order book not synced → probability 0, with a note. No number is offered
  from a book that cannot be trusted.

## A bug worth remembering

`fading_bounces` reported 0.0 for a level being tested three times. The
first implementation recorded a bounce for every consecutive bar sitting
*at* the level, each resetting the extreme to the level itself, so every
bounce measured zero and every one was filtered out as empty. A bounce is
now closed by the **return** to the level, and only a bar that genuinely
left the zone can contribute. It was invisible in unit tests and obvious
in replay: it was the only feature that never moved.
