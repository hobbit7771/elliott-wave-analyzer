"""Bybit USDT perpetual (linear) REST client - the automatic fallback data
source when Binance is unreachable (regional block, or an inherited IP
ban - see rest_client.py and dashboard/server.py's shared backoff).

WHY BYBIT SPECIFICALLY: it's a second major exchange with a fully public,
no-API-key-required REST API for historical klines and the symbol list,
documented schema, and it is NOT the same company/infrastructure as
Binance - so an IP-level ban or regional block against Binance has no
bearing on whether Bybit's API is reachable from the same server. This
does not fix a Binance-side ban; it routes around it so the dashboard's
analysis/backtest features keep working on real market data regardless.

Bybit's public kline endpoint covers minute-granularity intervals that
overlap this engine's tradeable timeframes: 1/3/5/15/30/60/240 minutes
(and D/W/M, unused here) - see `_INTERVAL_MAP` below for the subset this
engine actually needs (M1/M3/M5/M15/H1/H4).

Schema reference (Bybit API v5, https://bybit-exchange.github.io/docs/v5/market/kline):
  GET /v5/market/kline?category=linear&symbol=BTCUSDT&interval=5&limit=200
  -> {"retCode": 0, "result": {"list": [[start, open, high, low, close,
      volume, turnover], ...]}}   # newest-first, all values as strings
  GET /v5/market/instruments-info?category=linear
  -> {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT",
      "status": "Trading", "contractType": "LinearPerpetual",
      "quoteCoin": "USDT"}, ...]}}

NETWORK NOTE: same caveat as rest_client.py - this build session's sandbox
blocks outbound access to real exchange APIs, so this client is written
and unit-tested against Bybit's documented schema with a mocked transport
(tests/test_market_data.py), not exercised against a live call here.
"""

from __future__ import annotations

import time
from typing import List, Optional

import httpx

from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe

_INTERVAL_MAP = {
    Timeframe.M1: "1", Timeframe.M3: "3", Timeframe.M5: "5",
    Timeframe.M15: "15", Timeframe.H1: "60", Timeframe.H4: "240",
}

MIN_REQUEST_INTERVAL_SECONDS = 0.2  # Bybit's public-endpoint limits are generous; still self-throttle defensively


class BybitFuturesREST:
    def __init__(self, base_url: str = "https://api.bybit.com", client: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._last_request_at: float = 0.0

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict) -> dict:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        resp = self._client.get(path, params=params)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            raise BybitAPIError(data.get("retCode"), data.get("retMsg", "unknown Bybit error"))
        return data["result"]

    def get_klines(self, symbol: str, timeframe: Timeframe, limit: int = 200) -> List[Candle]:
        if timeframe not in _INTERVAL_MAP:
            raise ValueError(f"Bybit client has no interval mapping for {timeframe}")
        result = self._get("/v5/market/kline", {
            "category": "linear", "symbol": symbol, "interval": _INTERVAL_MAP[timeframe],
            "limit": min(limit, 1000),
        })
        rows = result.get("list", [])
        candles = [self._parse_kline(row, timeframe) for row in rows]
        candles.reverse()  # Bybit returns newest-first; this engine expects chronological order
        return candles

    @staticmethod
    def _parse_kline(row: list, timeframe: Timeframe) -> Candle:
        # Bybit kline row: [start, open, high, low, close, volume, turnover]
        start = int(row[0])
        return Candle(
            timeframe=timeframe,
            open_time=start,
            close_time=start + timeframe.seconds * 1000 - 1,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            closed=True,
        )

    def list_symbols(self, quote_coin: Optional[str] = "USDT") -> List[str]:
        """All actively-tradeable linear (USDT-margined) perpetual symbols,
        mirroring BinanceFuturesREST.list_symbols()'s contract so callers
        can swap one for the other transparently."""
        result = self._get("/v5/market/instruments-info", {"category": "linear"})
        symbols = []
        for s in result.get("list", []):
            if s.get("status") != "Trading":
                continue
            if s.get("contractType") != "LinearPerpetual":
                continue
            if quote_coin and s.get("quoteCoin") != quote_coin:
                continue
            symbols.append(s["symbol"])
        return sorted(symbols)


class BybitAPIError(Exception):
    def __init__(self, ret_code: Optional[int], ret_msg: str):
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        super().__init__(f"Bybit API error {ret_code}: {ret_msg}")
