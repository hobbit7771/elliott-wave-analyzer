"""Historical candles for the chart, from Bybit REST. Bybit only.

The chart loads history ONCE and is updated incrementally by the socket
after that - never by refetching 1500 candles a second. This module is
the "once" half; `kline.*` on the WebSocket is the other half.

Its own small client rather than the project's `BybitFuturesREST`: that
one belongs to the analyser and returns the analyser's `Candle` type, and
importing it would put a dependency across the isolation boundary for the
sake of forty lines. See docs/lead-engine/ARCHITECTURE.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Bybit's interval notation. The brief's list, plus the daily it asks for
# "if possible".
INTERVALS: Dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "4h": "240", "1d": "D",
}
INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

MAX_LIMIT = 1000
DEFAULT_LIMIT = 500


def normalize_interval(raw: str) -> Optional[str]:
    """Accept either spelling - `5m` or Bybit's bare `5`."""
    key = str(raw or "").strip().lower()
    if key in INTERVALS:
        return key
    for label, bybit in INTERVALS.items():
        if key == bybit.lower():
            return label
    return None


def fetch_candles(symbol: str, interval: str, limit: int = DEFAULT_LIMIT,
                  base_url: str = "https://api.bybit.com",
                  client: Optional[httpx.Client] = None,
                  timeout: float = 15.0) -> List[Dict[str, Any]]:
    """Closed candles, oldest first, in the shape a chart library wants.

    Returns [] rather than raising: a chart with no history is a chart
    that says so, and an exception here would take down a page that is
    otherwise perfectly able to show live ticks.

    Bybit returns newest-first and includes the FORMING candle. Both are
    handled here so the caller never has to think about it: the list is
    reversed, and each row carries `closed` so the chart knows which bar
    the socket will keep updating."""
    label = normalize_interval(interval)
    if label is None:
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.get(f"{base_url.rstrip('/')}/v5/market/kline",
                        params={"category": "linear", "symbol": symbol.upper(),
                                "interval": INTERVALS[label], "limit": limit})
        if resp.status_code != 200:
            logger.warning("lead_engine candles: %s %s -> %s", symbol, label,
                           resp.status_code)
            return []
        rows = ((resp.json() or {}).get("result") or {}).get("list") or []
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.warning("lead_engine candles: %s %s failed: %s", symbol, label, exc)
        return []
    finally:
        if client is None:
            http.close()

    seconds = INTERVAL_SECONDS[label]
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            start_ms = int(row[0])
            out.append({
                # Seconds, because that is what lightweight-charts wants
                # and converting in one place beats converting in three.
                "time": start_ms // 1000,
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
                "closed": True,
            })
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda c: c["time"])
    # The newest row is the bar still forming; mark it so the chart knows
    # the socket will keep updating that one rather than appending.
    if out:
        out[-1]["closed"] = False
    return [dict(candle, interval=label, interval_seconds=seconds) for candle in out]


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """Exponential moving average, seeded with a simple mean.

        alpha   = 2 / (period + 1)
        EMA_t   = alpha * close_t + (1 - alpha) * EMA_(t-1)

    Computed here as well as in the browser so the two can be checked
    against each other - `tests/test_lead_engine_chart.py` asserts they
    agree to within a rounding error on the same input, which is the only
    way to know the chart is drawing the same line the engine would.

    The first `period - 1` entries are None rather than a partial average:
    an EMA needs its seed, and emitting a number before it has one is how
    the first bars of every indicator end up wrong."""
    period = int(period)
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    alpha = 2.0 / (period + 1.0)
    out: List[Optional[float]] = [None] * (period - 1)
    seed = sum(values[:period]) / float(period)
    out.append(seed)
    previous = seed
    for value in values[period:]:
        previous = alpha * float(value) + (1.0 - alpha) * previous
        out.append(previous)
    return out


def ema_series(candles: List[Dict[str, Any]], periods=(9, 18, 50, 200)) -> Dict[str, List[Dict]]:
    """One EMA line per period, in the chart's point shape."""
    closes = [float(c["close"]) for c in candles]
    times = [int(c["time"]) for c in candles]
    out: Dict[str, List[Dict]] = {}
    for period in periods:
        line = ema(closes, period)
        out[f"ema{period}"] = [{"time": t, "value": round(v, 10)}
                               for t, v in zip(times, line) if v is not None]
    return out
