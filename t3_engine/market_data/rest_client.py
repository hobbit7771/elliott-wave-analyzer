"""Binance USDT-M Futures REST client (spec section 2).

Covers: historical klines (>=1000 candles per TF), Open Interest, funding
rate history, long/short ratio, and aggTrades (used both to backfill
sub-minute candles for a recent lookback window, and to build the taker
buy/sell volume split klines alone don't expose per-trade).

RATE LIMITS (Binance USDT-M Futures, per their documented values as of
this writing - Binance can and does change these, so treat the numbers
below as "the ballpark this client is designed around", not a live-fetched
guarantee; verify against https://developer.binance.com/docs/derivatives/usds-margined-futures/general-info
if behavior seems off):
  - IP request-weight budget: 2400 weight/minute. Binance echoes the
    count so far in the `X-MBX-USED-WEIGHT-1M` response header - this
    client records it (see `last_used_weight`) so callers can watch it
    without guessing.
  - Endpoint weights used here are all cheap: exchangeInfo=1, klines up to
    ~10 (scales with `limit`), openInterest/fundingRate/aggTrades typically
    1-5. Nothing in this client comes close to the 2400 budget on its own.
  - Exceeding the weight budget -> HTTP 429; continuing to hit the API
    after a 429 is what escalates to HTTP 418 ("I'm a teapot" - Binance's
    documented IP-ban response), with the ban duration itself escalating
    on repeat offenses (their docs describe it scaling from minutes up to
    days). See dashboard/server.py for the process-wide backoff that reacts
    to a 418/429 once Binance actually sends one.
  - Independently of all that, this client also enforces its own minimum
    spacing between outbound requests (`MIN_REQUEST_INTERVAL_SECONDS`)
    so it never becomes the reason a shared IP gets rate-limited in the
    first place - a purely defensive measure, not a reaction to anything
    Binance has told us yet.

NETWORK NOTE: this client is written and unit-tested against Binance's
documented REST schema using a mocked HTTP transport (see
tests/test_market_data.py) - this sandboxed build session's outbound
network policy blocks `fapi.binance.com` directly (confirmed via a 403 at
the proxy), so a live call has not been exercised here. The request/parsing
logic itself has no sandbox-specific workaround in it; it will work as
soon as it's run somewhere with normal internet access.
"""

from __future__ import annotations

import time
from typing import List, Optional

import httpx

from t3_engine.candle_builder.aggregator import Trade
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe

_INTERVAL_MAP = {
    Timeframe.M1: "1m", Timeframe.M3: "3m", Timeframe.M5: "5m",
    Timeframe.M15: "15m", Timeframe.H1: "1h", Timeframe.H4: "4h",
}

IP_WEIGHT_BUDGET_PER_MINUTE = 2400  # Binance's documented per-IP budget
MIN_REQUEST_INTERVAL_SECONDS = 0.5  # self-imposed floor: at most 2 req/s


class BinanceFuturesREST:
    def __init__(self, base_url: str = "https://fapi.binance.com", client: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self.last_used_weight: Optional[int] = None
        self._last_request_at: float = 0.0

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        """Every read in this client funnels through here so the self-
        imposed request spacing and weight tracking apply uniformly,
        instead of being something each method has to remember to do."""
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        resp = self._client.get(path, params=params)
        self._last_request_at = time.monotonic()
        weight_header = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        if weight_header is not None:
            try:
                self.last_used_weight = int(weight_header)
            except ValueError:
                pass
        resp.raise_for_status()
        return resp

    @property
    def weight_budget_remaining(self) -> Optional[int]:
        if self.last_used_weight is None:
            return None
        return max(IP_WEIGHT_BUDGET_PER_MINUTE - self.last_used_weight, 0)

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
        resp = self._get("/fapi/v1/klines", params=params)
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
        resp = self._get("/fapi/v1/aggTrades", params=params)
        raw = resp.json()
        return [Trade(timestamp=int(t["T"]), price=float(t["p"]), quantity=float(t["q"]),
                       is_buyer_maker=bool(t["m"])) for t in raw]

    def get_open_interest(self, symbol: str) -> float:
        resp = self._get("/fapi/v1/openInterest", params={"symbol": symbol})
        return float(resp.json()["openInterest"])

    def get_funding_rate_history(self, symbol: str, limit: int = 100) -> List[dict]:
        resp = self._get("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": limit})
        return resp.json()

    def get_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 30) -> List[dict]:
        resp = self._get("/futures/data/globalLongShortAccountRatio",
                          params={"symbol": symbol, "period": period, "limit": limit})
        return resp.json()

    def get_taker_buy_sell_volume(self, symbol: str, period: str = "5m", limit: int = 30) -> List[dict]:
        resp = self._get("/futures/data/takerlongshortRatio",
                          params={"symbol": symbol, "period": period, "limit": limit})
        return resp.json()

    def get_exchange_info(self) -> dict:
        resp = self._get("/fapi/v1/exchangeInfo")
        return resp.json()

    def list_symbols(self, quote_asset: Optional[str] = None, contract_type: str = "PERPETUAL") -> List[str]:
        """All actively-tradeable USDT-M futures symbols (e.g. BTCUSDT),
        used to populate the dashboard's symbol picker so users choose from
        what Binance actually lists instead of guessing a format. Filters
        to `status == "TRADING"` (delisted/pre-launch symbols excluded) and
        `contractType == "PERPETUAL"` by default (this app only trades
        perpetuals, not dated futures)."""
        info = self.get_exchange_info()
        symbols = []
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            if contract_type and s.get("contractType") != contract_type:
                continue
            if quote_asset and s.get("quoteAsset") != quote_asset:
                continue
            symbols.append(s["symbol"])
        return sorted(symbols)
