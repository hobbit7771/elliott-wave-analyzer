#!/usr/bin/env python3
"""CLI entry point: run the T3 engine live against Binance USDT-M Futures
in PAPER mode (simulated fills, real market data).

    python run_paper_trading.py --symbol BTCUSDT

REQUIRES real outbound network access to fstream.binance.com /
fapi.binance.com. This project's own build/CI sandbox blocks that host at
the network policy layer (see README "Known limitations" for the exact
error) - the code path below is real and unit-tested against a fake trade
stream (tests/test_pipeline_live_loop.py), but has not been run against a
live socket in this session. Run it yourself in an environment with normal
internet access.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from t3_engine.pipeline.live_loop import LiveTradingEngine


async def _main_async(symbol: str, equity: float, threshold: float, log_dir: str) -> None:
    engine = LiveTradingEngine(symbol=symbol, initial_equity=equity,
                                entry_confidence_threshold=threshold, log_dir=log_dir)
    print(f"Starting PAPER trading engine for {symbol} (equity={equity}, threshold={threshold})")
    print(f"Decision log: {log_dir}/signals.jsonl")
    await engine.run_live_binance()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="T3 PAPER trading engine (live Binance data, simulated fills)")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--threshold", type=float, default=75.0)
    parser.add_argument("--log-dir", default="./logs")
    args = parser.parse_args()

    try:
        asyncio.run(_main_async(args.symbol, args.equity, args.threshold, args.log_dir))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
