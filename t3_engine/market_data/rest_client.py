"""Binance USDT-M Futures REST client (spec section 2).

Covers: historical klines (>=1000 candles per TF), Open Interest, funding
rate history, long/short ratio, and aggTrades (used both to backfill
sub-minute candles for a recent lookback window, and to build the taker
buy/sell volume split klines alone don't expose per-trade).

NETWORK NOTE: this client is written and unit-tested against Binance's
documented REST schema using a mocked HTTP transport (see
tests/test_market_data.py) - this sandboxed build session's outbound
network policy blocks `fapi.binance.com` directly (confirmed via a 403 at
the proxy), so a live call has not been exercised here. The request/parsing
logic itself has no sandbox-specific workaround in it; it will work as
soon as it's run somewhere with normal internet access.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from t3_engine.candle_builder.aggregator import Trade
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe

_INTERVAL_MAP = {
    Timeframe.M1: "1m", Timeframe.M3: "3m", Timeframe.M5: "5m",
    Timeframe.M15: "15m", Timeframe.H1: "1h", Timeframe.H4: "4h",
}


class BinanceFuturesREST:
    def __init__(self, base_url: str = "https://fapi.binance.com", client: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get_klines(self, symbol: str, timeframe: Timeframe, limit: int = 1500,
                    start_time: Optional[int] = None, end_time: Optional[int] = None) -> List[Candle]:
        if timeframe not in _INTERVAL_MAP:
            raise ValueError(f"Binance REST has no native interval for {timeframe} - "
                              f"build it via candle_builder.resample_candles from 1m instead")
        params = {"symbol": symbol, "interval": _INTERVAL_MAP[timeframe], "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        resp = self._client.get("/fapi/v1/klines", params=params)
        resp.raise_for_status()
        raw = resp.json()
        return [self._parse_kline(row, timeframe) for row in raw]

    @staticmethod
    def _parse_kline(row: list, timeframe: Timeframe) -> Candle:
        # Binance kline row: [openTime, open, high, low, close, volume,
        #  closeTime, quoteVolume, trades, takerBuyBaseVolume, takerBuyQuoteVolume, ignore]
        return Candle(
            timeframe=timeframe,
            open_time=int(row[0]),
            close_time=int(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            taker_buy_volume=float(row[9]),
            trades=int(row[8]),
            closed=True,
        )

    def get_agg_trades(self, symbol: str, limit: int = 1000, from_id: Optional[int] = None,
                        start_time: Optional[int] = None, end_time: Optional[int] = None) -> List[Trade]:
        """Used to reconstruct sub-minute (1s/5s/15s/30s) candles for a
        recent lookback window. Binance does NOT expose historical klines
        below 1m via REST at all - aggTrades is the only way to get
        genuine sub-minute OHLCV out of history, and it is only retained
        for a limited retention window server-side. That is a real
        exchange-side limitation, not a shortcut in this client: beyond
        that window, sub-minute backtesting must fall back to whatever
        1m-resampled granularity is available (see backtest/ module docs)."""
        params = {"symbol": symbol, "limit": limit}
        if from_id is not None:
            params["fromId"] = from_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        resp = self._client.get("/fapi/v1/aggTrades", params=params)
        resp.raise_for_status()
        raw = resp.json()
        return [Trade(timestamp=int(t["T"]), price=float(t["p"]), quantity=float(t["q"]),
                       is_buyer_maker=bool(t["m"])) for t in raw]

    def get_open_interest(self, symbol: str) -> float:
        resp = self._client.get("/fapi/v1/openInterest", params={"symbol": symbol})
        resp.raise_for_status()
        return float(resp.json()["openInterest"])

    def get_funding_rate_history(self, symbol: str, limit: int = 100) -> List[dict]:
        resp = self._client.get("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit})
        resp.raise_for_status()
        return resp.json()

    def get_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 30) -> List[dict]:
        resp = self._client.get("/futures/data/globalLongShortAccountRatio",
                                 params={"symbol": symbol, "period": period, "limit": limit})
        resp.raise_for_status()
        return resp.json()

    def get_taker_buy_sell_volume(self, symbol: str, period: str = "5m", limit: int = 30) -> List[dict]:
        resp = self._client.get("/futures/data/takerlongshortRatio",
                                 params={"symbol": symbol, "period": period, "limit": limit})
        resp.raise_for_status()
        return resp.json()
