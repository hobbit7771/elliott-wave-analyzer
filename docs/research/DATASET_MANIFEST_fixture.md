# DATASET_MANIFEST: tests/fixtures/bybit_capture_injusdt.jsonl.gz

* classification : **SYNTHETIC**
* source         : checked into this repository, not downloaded from anywhere
* provenance     : added in commit 661b036 (2026-09-15) and described there as "twenty minutes of six Bybit topics ... saved rather than generated". That description is wrong; see the classification.
* market         : Bybit USDT perpetual (linear)
* symbols        : BTCUSDT, INJUSDT
* period (UTC ms): 1700000000000 -> 1700001199500 (0.333 h)
* segments       : 0
* frames         : 10288 (0 dropped by the recorder)
* sha256         : 8d7c47feb58045f84170e9236b54213b15ad5d39a020d9c5f095b054a3973a2a

## Topic mix
* publicTrade: 7216
* orderbook: 2402
* tickers: 600
* allLiquidation: 70

## Quality
* book_frames: 2402
* book_updates_per_second: 2.003
* distinct_exchange_stamps: 2400
* frames: 10288
* gaps_over_30s: 0
* gaps_over_5s: 0
* max_gap_ms: 500
* median_gap_ms: 0
* median_levels_per_update: 80.0
* p95_gap_ms: 500
* receive_timestamps_present_pct: 100.0
* trade_frames: 7216
* trades_per_second: 6.016

## Warnings
* every timestamp falls on an exact 500ms grid - real exchange stamps are irregular
* the capture begins at exactly 1700000000000, a number somebody typed rather than a moment that happened

## Verdict

This file is a HAND-BUILT SCENARIO, not a recording.

It remains a good deterministic regression test for the ingest pipeline -
the same bytes give the same report, so a change in the report is a
change in the engine. It is worthless as evidence about how a market
behaves or about what an order would have paid, and no execution result
in this project may cite it.
