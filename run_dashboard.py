#!/usr/bin/env python3
"""CLI entry point: serve the T3 dashboard (FastAPI + lightweight-charts).

    python run_dashboard.py --port 8000

Then open http://localhost:8000 - it runs the synthetic demo fixture
through the full backtest pipeline by default (see dashboard/server.py for
why), or pass source=binance&symbol=BTCUSDT in the UI for real data.
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="T3 dashboard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("t3_engine.dashboard.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
