import json

import httpx
import pytest

from t3_engine.common.types import Timeframe
from t3_engine.market_data.bybit_rest_client import BybitAPIError, BybitFuturesREST
from t3_engine.market_data.bybit_ws_client import BybitFuturesWebSocketClient, parse_taker_side_is_buyer_maker
from t3_engine.market_data.rest_client import BinanceFuturesREST
from t3_engine.market_data.ws_client import BinanceFuturesWebSocketClient


def make_rest_client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://fapi.binance.com")
    return BinanceFuturesREST(client=http_client)


def test_rest_client_tracks_used_weight_header():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openInterest": "100", "symbol": "BTCUSDT"},
                               headers={"X-MBX-USED-WEIGHT-1M": "42"})

    client = make_rest_client(handler)
    assert client.last_used_weight is None
    client.get_open_interest("BTCUSDT")
    assert client.last_used_weight == 42
    from t3_engine.market_data.rest_client import IP_WEIGHT_BUDGET_PER_MINUTE
    assert client.weight_budget_remaining == IP_WEIGHT_BUDGET_PER_MINUTE - 42


def test_rest_client_enforces_minimum_request_spacing():
    from t3_engine.market_data.rest_client import MIN_REQUEST_INTERVAL_SECONDS
    import time

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openInterest": "1", "symbol": "BTCUSDT"})

    client = make_rest_client(handler)
    client.get_open_interest("BTCUSDT")
    start = time.monotonic()
    client.get_open_interest("BTCUSDT")
    elapsed = time.monotonic() - start
    assert elapsed >= MIN_REQUEST_INTERVAL_SECONDS * 0.9  # allow small scheduling jitter


def test_get_klines_parses_binance_row_format():
    sample_row = [1620000000000, "100.5", "110.2", "99.1", "105.0", "1234.5",
                  1620000059999, "130000.0", 42, "600.0", "63000.0", "0"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        assert "symbol=BTCUSDT" in str(request.url)
        assert "interval=5m" in str(request.url)
        return httpx.Response(200, json=[sample_row])

    client = make_rest_client(handler)
    candles = client.get_klines("BTCUSDT", Timeframe.M5, limit=10)
    assert len(candles) == 1
    c = candles[0]
    assert c.open == 100.5
    assert c.high == 110.2
    assert c.low == 99.1
    assert c.close == 105.0
    assert c.volume == 1234.5
    assert c.taker_buy_volume == 600.0
    assert c.trades == 42
    assert c.closed is True


def test_get_klines_rejects_unsupported_native_interval():
    client = make_rest_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        client.get_klines("BTCUSDT", Timeframe.S1, limit=10)


def test_get_agg_trades_parses_taker_side():
    sample = [{"a": 1, "p": "100.0", "q": "2.0", "T": 1620000000000, "m": True}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/aggTrades"
        return httpx.Response(200, json=sample)

    client = make_rest_client(handler)
    trades = client.get_agg_trades("BTCUSDT", limit=5)
    assert len(trades) == 1
    assert trades[0].is_buyer_maker is True
    assert trades[0].taker_buy_qty == 0.0  # buyer was maker -> taker sold


def test_get_open_interest_parses_value():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openInterest": "12345.6", "symbol": "BTCUSDT"})

    client = make_rest_client(handler)
    assert client.get_open_interest("BTCUSDT") == 12345.6


def test_list_symbols_filters_trading_perpetuals():
    sample = {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "DELISTEDUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BTCUSDT_240329", "status": "TRADING", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/exchangeInfo"
        return httpx.Response(200, json=sample)

    client = make_rest_client(handler)
    symbols = client.list_symbols()
    assert symbols == ["BTCUSDT", "ETHUSDT"]


def test_rest_client_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, json={"code": -1121, "msg": "bad symbol"})

    client = make_rest_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_klines("NOTASYMBOL", Timeframe.M5)


# ---- Bybit REST client (fallback data source when Binance is unreachable) ----

def make_bybit_client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://api.bybit.com")
    return BybitFuturesREST(client=http_client)


def test_bybit_get_klines_parses_row_format_and_reverses_to_chronological_order():
    # Bybit returns newest-first; row = [start, open, high, low, close, volume, turnover]
    rows = [
        ["1620000300000", "106.0", "107.0", "105.0", "106.5", "50.0", "5000"],
        ["1620000000000", "100.5", "110.2", "99.1", "105.0", "1234.5", "120000"],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/market/kline"
        assert "category=linear" in str(request.url)
        assert "symbol=BTCUSDT" in str(request.url)
        assert "interval=5" in str(request.url)
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": rows}})

    client = make_bybit_client(handler)
    candles = client.get_klines("BTCUSDT", Timeframe.M5, limit=10)
    assert len(candles) == 2
    assert candles[0].open_time == 1620000000000  # oldest first after reversal
    assert candles[0].close == 105.0
    assert candles[1].open_time == 1620000300000
    assert candles[1].close == 106.5


def test_bybit_get_klines_rejects_unsupported_timeframe():
    client = make_bybit_client(lambda r: httpx.Response(200, json={"retCode": 0, "result": {"list": []}}))
    with pytest.raises(ValueError):
        client.get_klines("BTCUSDT", Timeframe.S1, limit=10)


def test_bybit_list_symbols_filters_trading_linear_perpetuals():
    sample = {
        "retCode": 0, "retMsg": "OK",
        "result": {"list": [
            {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT"},
            {"symbol": "ETHUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT"},
            {"symbol": "DELISTEDUSDT", "status": "Closed", "contractType": "LinearPerpetual", "quoteCoin": "USDT"},
            {"symbol": "BTCUSD", "status": "Trading", "contractType": "InversePerpetual", "quoteCoin": "USD"},
        ]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/market/instruments-info"
        return httpx.Response(200, json=sample)

    client = make_bybit_client(handler)
    assert client.list_symbols() == ["BTCUSDT", "ETHUSDT"]


def test_bybit_raises_on_nonzero_ret_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 10001, "retMsg": "invalid symbol", "result": {}})

    client = make_bybit_client(handler)
    with pytest.raises(BybitAPIError):
        client.get_klines("NOTASYMBOL", Timeframe.M5)


def test_bybit_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"retCode": 403, "retMsg": "forbidden"})

    client = make_bybit_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_klines("BTCUSDT", Timeframe.M5)


# ---- WebSocket client ----

class FakeWSConnection:
    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


def fake_connect_factory(messages):
    def _connect(url):
        return FakeWSConnection(messages)
    return _connect


def test_ws_build_stream_url_combines_symbols_and_streams():
    client = BinanceFuturesWebSocketClient(symbols=["BTCUSDT"], streams=["aggTrade", "bookTicker"],
                                            on_message=None, connect_fn=lambda url: None)
    url = client.build_stream_url()
    assert "btcusdt@aggTrade" in url
    assert "btcusdt@bookTicker" in url


@pytest.mark.asyncio
async def test_ws_dedups_and_detects_gaps():
    received = []

    async def on_message(stream, data):
        received.append(data)

    gaps = []

    async def on_gap(symbol, prev_id, new_id):
        gaps.append((symbol, prev_id, new_id))

    messages = [
        json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "100"}}),
        json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "100"}}),  # dup
        json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "a": 5, "p": "101"}}),  # gap
    ]
    client = BinanceFuturesWebSocketClient(symbols=["BTCUSDT"], streams=["aggTrade"], on_message=on_message,
                                            on_gap_detected=on_gap, connect_fn=fake_connect_factory(messages))
    await client.run(max_iterations=3)
    assert len(received) == 2  # duplicate dropped
    assert gaps == [("btcusdt", 1, 5)]


@pytest.mark.asyncio
async def test_ws_reconnect_backoff_on_error_is_raised_in_test_mode():
    async def on_message(stream, data):
        pass

    def bad_connect(url):
        raise ConnectionError("boom")

    client = BinanceFuturesWebSocketClient(symbols=["BTCUSDT"], streams=["aggTrade"], on_message=on_message,
                                            connect_fn=bad_connect)
    with pytest.raises(ConnectionError):
        await client.run(max_iterations=1)


# ---- Bybit WebSocket client (live-data fallback, see live_loop.py's run_live) ----

def test_parse_taker_side_is_buyer_maker_matches_binance_semantics():
    # Bybit S="Sell" means the taker sold -> same real event as Binance's
    # m=True ("buyer is maker", i.e. the taker sold); S="Buy" -> m=False.
    assert parse_taker_side_is_buyer_maker("Sell") is True
    assert parse_taker_side_is_buyer_maker("Buy") is False


@pytest.mark.asyncio
async def test_bybit_ws_subscribes_on_connect():
    messages = [json.dumps({"topic": "publicTrade.BTCUSDT",
                             "data": [{"T": 1620000000000, "s": "BTCUSDT", "S": "Buy", "p": "100.5", "v": "2.0"}]})]
    conn_holder = {}

    def connect(url):
        conn = FakeWSConnection(messages)
        conn_holder["conn"] = conn
        return conn

    received = []

    async def on_message(item):
        received.append(item)

    client = BybitFuturesWebSocketClient(symbols=["BTCUSDT"], on_message=on_message, connect_fn=connect)
    await client.run(max_iterations=1)

    assert len(conn_holder["conn"].sent) == 1
    sent = json.loads(conn_holder["conn"].sent[0])
    assert sent == {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}
    assert len(received) == 1
    assert received[0]["p"] == "100.5"


@pytest.mark.asyncio
async def test_bybit_ws_ignores_non_trade_topics():
    messages = [
        json.dumps({"success": True, "op": "subscribe"}),  # subscribe ack, not a trade
        json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{"T": 1, "s": "BTCUSDT", "S": "Sell", "p": "1", "v": "1"}]}),
    ]
    received = []

    async def on_message(item):
        received.append(item)

    client = BybitFuturesWebSocketClient(symbols=["BTCUSDT"], on_message=on_message,
                                          connect_fn=fake_connect_factory(messages))
    await client.run(max_iterations=2)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_bybit_ws_reconnect_backoff_on_error_is_raised_in_test_mode():
    async def on_message(item):
        pass

    def bad_connect(url):
        raise ConnectionError("boom")

    client = BybitFuturesWebSocketClient(symbols=["BTCUSDT"], on_message=on_message, connect_fn=bad_connect)
    with pytest.raises(ConnectionError):
        await client.run(max_iterations=1)
