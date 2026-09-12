#!/usr/bin/env python3
"""CLI entry point: run the T3 backtester and print a metrics report.

Examples:
    python run_backtest.py                                # synthetic demo fixture
    python run_backtest.py --source bybit --symbol BTCUSDT --limit 1000

NOTE: --source bybit requires outbound network access to api.bybit.com,
which this project's own build/CI sandbox does not have (see README
"Known limitations"). It will work in a normal environment.
"""

from __future__ import annotations

import argparse
import sys

from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.backtest.metrics import compute_metrics, compute_metrics_by_wave
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.types import Timeframe
from t3_engine.market_data.bybit_rest_client import BybitFuturesREST


def _print_metrics(title: str, m) -> None:
    print(f"\n-- {title} --")
    print(f"  trades:        {m.trades}")
    print(f"  winrate:       {m.winrate:.1%}")
    print(f"  profit factor: {m.profit_factor:.2f}" if m.profit_factor is not None else "  profit factor: n/a")
    print(f"  expectancy:    {m.expectancy:.4f}")
    print(f"  sharpe:        {m.sharpe:.2f}" if m.sharpe is not None else "  sharpe:        n/a")
    print(f"  sortino:       {m.sortino:.2f}" if m.sortino is not None else "  sortino:       n/a")
    print(f"  max drawdown:  {m.max_drawdown:.1%}")
    print(f"  avg R / med R: {m.average_r:.2f} / {m.median_r:.2f}")
    print(f"  avg MAE/MFE:   {m.mae_avg:.4f} / {m.mfe_avg:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="T3 Elliott Wave backtester")
    parser.add_argument("--source", choices=["synthetic", "bybit"], default="synthetic")
    parser.add_argument("--symbol", default="SYNTHETIC-DEMO")
    parser.add_argument("--cycles", type=int, default=3, help="synthetic source only")
    parser.add_argument("--limit", type=int, default=1000, help="bybit source only: candles to fetch")
    parser.add_argument("--threshold", type=float, default=75.0)
    parser.add_argument("--equity", type=float, default=10_000.0)
    args = parser.parse_args()

    if args.source == "bybit":
        rest = BybitFuturesREST()
        try:
            candles = rest.get_klines(args.symbol, Timeframe.M5, limit=args.limit)
        finally:
            rest.close()
    else:
        candles = generate_synthetic_series(num_cycles=args.cycles)
        print("NOTE: using the SYNTHETIC demo fixture (backtest/synthetic_data.py), not real market data. "
              "Pass --source bybit for a real historical run once you have network access to Bybit.")

    engine = BacktestEngine(BacktestConfig(symbol=args.symbol, initial_equity=args.equity,
                                            entry_confidence_threshold=args.threshold))
    result = engine.run(candles)

    print(f"\nSymbol: {args.symbol}  Candles: {len(candles)}  Signals evaluated: {len(result['signals'])}")
    accepted = [s for s in result["signals"] if s.decision == "SIGNAL_ACCEPTED"]
    rejected = [s for s in result["signals"] if s.decision == "SIGNAL_REJECTED"]
    print(f"Accepted: {len(accepted)}  Rejected: {len(rejected)}")
    print(f"Final equity: {result['final_equity']:.2f} (started at {args.equity:.2f})")

    _print_metrics("Overall", compute_metrics(result["closed_positions"], args.equity))
    for label, m in compute_metrics_by_wave(result["closed_positions"], args.equity).items():
        _print_metrics(label, m)

    return 0


if __name__ == "__main__":
    sys.exit(main())
