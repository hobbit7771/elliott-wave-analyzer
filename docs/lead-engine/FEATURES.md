# Features

Every number below is computed from Bybit data by deterministic code. No
model, no API, no provider. Windows are keyed on the **exchange**
timestamp, never on arrival time — a 400ms network hiccup would otherwise
compress four seconds of trades into one window and print a velocity spike
that never happened, and replay would produce different features from the
same recorded events.

## Trade flow (`trade_flow.py`)

Windows: **250ms, 1s, 3s, 5s, 15s, 30s, 60s, 5m** — one deque per stream,
each window answered by walking back from the newest entry.

```
delta       = taker_buy_volume − taker_sell_volume
delta_ratio = buy_volume / max(sell_volume, 1e-9)
```

Velocity: trades/sec, volume/sec, notional/sec, plus acceleration (this
second's trade count minus the previous second's).

**The z-score is against this instrument's own recent history**, not a
constant: forty trades a second is dead for BTCUSDT and a stampede for
FILUSDT. One reading is banked per whole second — sampling per trade would
make the history denser exactly when trading is fast, which is the bias
the z-score exists to remove. Thresholds 2.0 / 3.0 (configurable) map to
`elevated` / `extreme`.

A **large** trade is a multiple of this instrument's own median trade size
(default 8×). Relative, never a fixed notional: a $50k print is enormous
in FILUSDT and unremarkable in BTCUSDT.

## CVD (`cvd.py`)

Running total of taker buy minus taker sell. Its value is the four-way
comparison with price, named rather than collapsed into a signed number:

| | CVD up | CVD down |
|---|---|---|
| **price up** | agreement | **divergence, bearish** — price rising on sellers crossing |
| **price down** | **divergence, bullish** — buyers crossing, price falls anyway | agreement |

Evaluated over 15s, 60s and 5m. `divergence()` reports the shortest window
showing one, because the point of this engine is to be early.

## Microprice (`microprice.py`)

The level says little on its own — one large resting bid does it. The
**drift** is the reading: deltas at 250ms, 1s, 3s and 5s, expressed in
basis points of the midpoint so INJUSDT at 5.8 and BTCUSDT at 60,000 land
on one scale. `bias` is bullish / bearish / neutral from the offset.

## Liquidations (`liquidation_engine.py`)

Windows 1s, 5s, 15s, 60s. States:

* `LONG_FLUSH` — longs force-sold, one side ≥65% of the notional
* `SHORT_SQUEEZE` — the mirror
* `CASCADE` — accelerating; forced sellers do not care what they get
* `EXHAUSTION` — it *was* fast and has decayed
* `NEUTRAL`

`EXHAUSTION` is measured against the **peak** 5s rate in the last minute,
not the minute's average. A four-second flush of 90k averaged over sixty
seconds is 1.5k/s, below every sensible threshold — comparing that average
to the threshold made the state unreachable, which is how the bug was
found.

The sign is contrarian on purpose: a long flush scores **negative** while
it accelerates and flips **positive** once it exhausts. An engine that
reads a flush as merely bearish sells the low of every one.

## Open interest (`oi_engine.py`)

| | OI up | OI down |
|---|---|---|
| **price up** | new longs — expansion | short covering |
| **price down** | new shorts | long liquidation / deleveraging |

The second cell matters most: a rally on falling open interest is people
buying back what they sold, and it stops when they are done. Squeezes and
deleveraging score at **half weight and against** the direction they
travel in — moves without new conviction behind them.

OI is folded into the liquidation component at 30%, rather than given a
tenth weight, so the nine published weights still sum to one.

## BTC lead-lag (`btc_leadlag.py`)

Returns are bucketed onto a **common one-second grid** before anything is
compared; comparing raw trade-to-trade returns across two instruments with
different trade rates measures the trade rates. Log returns, because they
add across buckets and are symmetric under the shifts the lag search does.

Rolling correlation over 120 buckets, then a cross-correlation over lags
0–8 buckets; the argmax is the lag estimate. **Below |0.25| correlation no
lag is offered** — a lag read off a correlation of 0.05 is the argmax of
noise.

`btc_impulse` is BTC's last three buckets in standard deviations of its
own recent noise. A 0.1% BTC move is enormous in a quiet hour and
unremarkable in a volatile one, so an unnormalised threshold fires on the
clock rather than on the market.

`lead_score = impulse × |correlation| × sign(correlation) × lead_weight`,
multiplied rather than summed because all three are necessary: BTC has to
be moving, the two have to be related, and BTC has to be ahead. Any one
missing makes the others irrelevant.

BTC does not lead itself: `state_for("BTCUSDT")` returns correlation
`None`, not 1.0.

## Structure (`smc_engine.py`, `elliott_state.py`)

Fractal swings (two bars each side) over **closed candles only** — the
newest bar can never be a swing, which is the no-lookahead guarantee at
this level. HH/HL/LH/LL, BOS (break with the trend), CHoCH (the first
break against it), sweep (wick through an extreme, close back inside),
FVG, order block, premium/discount.

`elliott_state.py` is a **context**, not a count. It prefers an injected
read-only reader of the project's own analysis if one is supplied, and
otherwise runs a small deterministic machine guarded by three hard rules
(wave 2 never past the start of 1, wave 4 never overlapping 1, wave 3
never the shortest). When a guard fails the count does not get rescued —
it re-anchors and starts again at wave 1. It carries 6% of the score,
which is the right size for a heuristic: enough to break a tie, never
enough to make a signal.

## Candles from trades (`candles.py`)

Bybit's kline topic pushes the **current** bar on subscribe, not history,
so an engine taking structure only from it has no swings for its first
several minutes — and therefore no levels and no pre-break score. Trades
are aggregated locally as well: 1-minute bars for structure, **15-second**
bars for levels. `SymbolState` uses whichever structure series has more
closed bars.

A bar closes only when a trade arrives in a later bucket. Closing on a
timer would produce bars no trade confirmed; closing the newest bar
speculatively is the lookahead everything here refuses.
