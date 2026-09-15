# The local L2 order book

Rebuilt from Bybit's `orderbook.50.{symbol}` snapshot + delta stream. No
REST polling per update — that is the whole reason this exists.

## Correctness before features

Bybit's deltas carry an update id `u` that increments by exactly one. That
detail is what lets the book know it is **right**, and it is the reason a
streamed book is safer than a polled one rather than merely cheaper.

Three ways the book refuses to serve numbers:

1. **Sequence gap.** `u` jumps. A delta applied over a gap produces a book
   that looks fine, quotes that look fine and an imbalance that is quietly
   wrong — and nothing downstream can detect it. The book desyncs and
   waits for a snapshot.
2. **Crossed book.** Best bid ≥ best ask. This cannot exist on an
   exchange, so a local book showing one is wrong — a missed removal, a
   mangled level. *Found in replay*, where a fixture that never removed
   stale bids produced a book quoting 5.90 bid against a 5.65 ask with
   **contiguous update ids** and a perfectly well-formed imbalance
   computed from it. The sequence check cannot see this, so it is checked
   directly.
3. **Not yet synced.** A delta arriving before any snapshot is refused
   rather than applied to an empty book.

While desynced, `metrics()` returns `synced: false` and no prices. The
pre-break engine offers no probability. The health monitor reports
`order book not synced` and the signal machine goes to `DATA_FAILURE`.

## What is computed

Best bid/ask, spread (absolute and bps), midpoint, bid/ask depth, and:

**OBI** at 1, 5, 10, 25 and 50 levels:

```
OBI(n) = (Σ bid size over n levels − Σ ask size over n levels)
       / (Σ bid size + Σ ask size)
```

**Weighted OBI** discounts each level by its distance from the midpoint.
Depth far from the touch is real but it is not what the next hundred
milliseconds trade against, and an unweighted 50-level imbalance is
dominated by size nobody will reach.

**Microprice** — size-weighted, by the *opposite* side's size:

```
microprice = (bid × ask_size + ask × bid_size) / (bid_size + ask_size)
```

A huge bid and a thin offer means the next print is likelier at the offer,
so the microprice sits **above** the midpoint. Getting this backwards is
the classic microprice bug and there is a test for it.

## Pulling vs. replenishment vs. absorption

The part that needs the trade stream. Size leaving the bid because it was
**bought** is not the same event as size leaving because it was
**cancelled**, and the second one is the interesting one.

`OrderBook.note_trade(side, qty)` is called from `SymbolState.on_trade`.
It records executed volume per side; `apply()` then subtracts it from the
observed decrease and calls only the remainder a pull:

```
removed   = Σ max(0, old_size − new_size)  across levels present before
added     = Σ max(0, new_size − old_size)
cancelled = max(0, removed − executed)      ← "pulling"
```

Without `note_trade`, every execution reads as a cancellation and the
engine reports constant pulling in exactly the conditions — heavy
trading — where pulling is supposed to mean something. `pulling` is one of
the heaviest features in the pre-break score, so this matters.

**Absorption** = `added / executed` on the side being hit: size being
replaced as fast as it is taken.

**Stacked liquidity** is a *run* of consecutive levels from the touch at
≥2× the median level size — a run, not a count of big levels scattered
through the book, because one large order five levels down says nothing
about the next tick.

**Walls** are levels ≥ `wall_multiple` × the median level size. Their
`persistence` is how long that price has carried one. A wall that
disappears while price never reached it is counted as a **cancellation**;
one that disappears after price traded to it is not.
