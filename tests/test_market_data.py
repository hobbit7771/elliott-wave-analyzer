import json

import httpx
import pytest

from t3_engine.common.types import Timeframe
from t3_engine.market_data.rest_client import BinanceFuturesREST
from t3_engine.market_data.ws_client import BinanceFuturesWebSocketClient


def make_rest_client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://fapi.binance.com")
    return BinanceFuturesREST(client=http_client)


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


def test_rest_client_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, json={"code": -1121, "msg": "bad symbol"})

    client = make_rest_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_klines("NOTASYMBOL", Timeframe.M5)


# ---- WebSocket client ----

class FakeWSConnection:
    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

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
