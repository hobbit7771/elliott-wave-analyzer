"""Bybit public WebSocket client - the live-data fallback for when
Binance's public WS isn't delivering trades (same rationale as
bybit_rest_client.py: a different exchange on different infrastructure,
unaffected by a Binance-side IP ban or regional block). See
pipeline/live_loop.py's `run_live()` for how this is used: it is only
reached after a watchdog decides Binance produced no trade in time.

Schema reference (Bybit API v5, https://bybit-exchange.github.io/docs/v5/websocket/public/trade):
  connect to wss://stream.bybit.com/v5/public/linear, then send
  {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}
  trade messages arrive as:
  {"topic": "publicTrade.BTCUSDT", "type": "snapshot",
   "data": [{"T": 1672304486868, "s": "BTCUSDT", "S": "Buy", "v": "0.001",
             "p": "16578.50", ...}], "ts": ...}
  `S` is the TAKER's side ("Buy"/"Sell") - the semantic equivalent of
  Binance aggTrade's inverted `m` flag (`m=true` means the buyer was the
  maker, i.e. the taker sold) - see `_parse_trade_message` below for the
  exact mapping so callers get a consistent `Trade.is_buyer_maker`
  regardless of which exchange actually supplied it.

NETWORK NOTE: same caveat as every other exchange client in this repo -
this sandboxed build session cannot open a socket to a live exchange, so
this is written and unit-tested against Bybit's documented v5 schema with
a fake connect_fn (tests/test_market_data.py), not exercised against a
live socket here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


class BybitFuturesWebSocketClient:
    def __init__(self, symbols: List[str], on_message: Callable[[dict], Awaitable[None]],
                 base_url: str = "wss://stream.bybit.com/v5/public/linear",
                 connect_fn=None, max_backoff_seconds: float = 60.0):
        """`on_message` receives one raw Bybit publicTrade data item per
        call (already unwrapped from the `data` list) - shaping it into
        this engine's `Trade` type is the caller's job (see
        pipeline/live_loop.py's `run_live_bybit`), matching how
        BinanceFuturesWebSocketClient hands its caller raw stream data."""
        self.symbols = [s.upper() for s in symbols]
        self.on_message = on_message
        self.base_url = base_url.rstrip("/")
        self.max_backoff_seconds = max_backoff_seconds
        self._connect_fn = connect_fn
        self._running = False

    def _resolve_connect_fn(self):
        if self._connect_fn is not None:
            return self._connect_fn
        import websockets  # local import: only required if actually running live
        return websockets.connect

    def _subscribe_message(self) -> str:
        return json.dumps({"op": "subscribe", "args": [f"publicTrade.{s}" for s in self.symbols]})

    async def _handle_raw_message(self, raw: str) -> None:
        envelope = json.loads(raw)
        topic = envelope.get("topic", "")
        if not topic.startswith("publicTrade."):
            return  # subscribe acks and pings also arrive on this socket
        for item in envelope.get("data", []):
            await self.on_message(item)

    async def run(self, max_iterations: Optional[int] = None) -> None:
        """Main reconnect loop - mirrors BinanceFuturesWebSocketClient.run()
        exactly (see its docstring for why max_iterations exists)."""
        connect_fn = self._resolve_connect_fn()
        backoff = 1.0
        self._running = True
        iterations = 0

        while self._running:
            try:
                async with connect_fn(self.base_url) as ws:
                    await ws.send(self._subscribe_message())
                    backoff = 1.0
                    logger.info("Bybit WS connected: %s", self.base_url)
                    async for raw in ws:
                        await self._handle_raw_message(raw)
                        iterations += 1
                        if max_iterations is not None and iterations >= max_iterations:
                            self._running = False
                            return
            except Exception as exc:  # noqa: BLE001 - reconnect on anything, log it
                logger.warning("Bybit WS disconnected (%s), reconnecting in %.1fs", exc, backoff)
                if max_iterations is not None:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)

    def stop(self) -> None:
        self._running = False


def parse_taker_side_is_buyer_maker(bybit_side: str) -> bool:
    """Bybit's `S` is the TAKER's side; Binance's aggTrade `m` flag is
    true when the BUYER was the maker (i.e. the taker sold). So Bybit
    `S == "Sell"` (taker sold) is the same real-world event as Binance
    `m == True` - this keeps `Trade.is_buyer_maker` meaning the same thing
    everywhere in the pipeline regardless of which exchange produced it."""
    return bybit_side == "Sell"
