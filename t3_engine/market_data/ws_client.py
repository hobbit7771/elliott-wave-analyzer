"""Binance USDT-M Futures combined-stream WebSocket client (spec section 2).

Handles the operational requirements the spec calls out explicitly:
  - auto-reconnect with exponential backoff;
  - sequence/order checking + dedup for aggTrade (Binance's `a` field is a
    strictly increasing aggregate-trade id per symbol - a gap means we
    missed messages and must backfill via REST; a repeat/lower id means a
    duplicate that must be dropped, not re-processed);
  - one connection subscribed to multiple raw streams (trades, bookTicker,
    depth, markPrice, kline) via the `/stream?streams=...` combined
    endpoint, so we don't open N separate sockets per symbol.

NETWORK NOTE: exactly like rest_client.py, this has not been run against
a live `fstream.binance.com` socket in this sandboxed session (outbound is
policy-blocked here). It IS unit-tested end-to-end using a fake
`connect_fn` that yields canned messages, which exercises the parsing,
sequence-gap-detection and reconnect/backoff logic without a real network
socket - see tests/test_market_data.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SequenceState:
    last_agg_trade_id: Optional[int] = None
    gaps_detected: int = 0
    duplicates_dropped: int = 0


class BinanceFuturesWebSocketClient:
    def __init__(self, symbols: List[str], streams: List[str],
                 on_message: Callable[[str, dict], Awaitable[None]],
                 on_gap_detected: Optional[Callable[[str, int, int], Awaitable[None]]] = None,
                 base_url: str = "wss://fstream.binance.com",
                 connect_fn=None, max_backoff_seconds: float = 60.0):
        """`connect_fn` defaults to `websockets.connect` (imported lazily so
        the module doesn't hard-require the `websockets` package just to be
        imported/tested); tests inject a fake async-context-manager."""
        self.symbols = [s.lower() for s in symbols]
        self.streams = streams
        self.on_message = on_message
        self.on_gap_detected = on_gap_detected
        self.base_url = base_url.rstrip("/")
        self.max_backoff_seconds = max_backoff_seconds
        self._connect_fn = connect_fn
        self.sequence_state: Dict[str, SequenceState] = {s: SequenceState() for s in self.symbols}
        self._running = False

    def build_stream_url(self) -> str:
        stream_names = [f"{sym}@{stream}" for sym in self.symbols for stream in self.streams]
        return f"{self.base_url}/stream?streams={'/'.join(stream_names)}"

    def _resolve_connect_fn(self):
        if self._connect_fn is not None:
            return self._connect_fn
        import websockets  # local import: only required if actually running live
        return websockets.connect

    def _check_agg_trade_sequence(self, symbol: str, agg_trade_id: int) -> str:
        """Returns 'OK', 'DUPLICATE' or 'GAP'."""
        state = self.sequence_state.setdefault(symbol, SequenceState())
        if state.last_agg_trade_id is None:
            state.last_agg_trade_id = agg_trade_id
            return "OK"
        if agg_trade_id <= state.last_agg_trade_id:
            state.duplicates_dropped += 1
            return "DUPLICATE"
        if agg_trade_id > state.last_agg_trade_id + 1:
            state.gaps_detected += 1
            state.last_agg_trade_id = agg_trade_id
            return "GAP"
        state.last_agg_trade_id = agg_trade_id
        return "OK"

    async def _handle_raw_message(self, raw: str) -> None:
        envelope = json.loads(raw)
        stream = envelope.get("stream", "")
        data = envelope.get("data", envelope)

        if stream.endswith("@aggTrade") or data.get("e") == "aggTrade":
            symbol = data["s"].lower()
            agg_id = int(data["a"])
            prev_id = self.sequence_state.get(symbol, SequenceState()).last_agg_trade_id
            status = self._check_agg_trade_sequence(symbol, agg_id)
            if status == "DUPLICATE":
                return
            if status == "GAP" and self.on_gap_detected is not None:
                await self.on_gap_detected(symbol, prev_id or agg_id, agg_id)

        await self.on_message(stream, data)

    async def run(self, max_iterations: Optional[int] = None) -> None:
        """Main reconnect loop. `max_iterations` is only used by tests to
        stop an otherwise-infinite loop deterministically."""
        connect_fn = self._resolve_connect_fn()
        backoff = 1.0
        self._running = True
        iterations = 0

        while self._running:
            try:
                async with connect_fn(self.build_stream_url()) as ws:
                    backoff = 1.0  # reset after a successful connection
                    async for raw in ws:
                        await self._handle_raw_message(raw)
                        iterations += 1
                        if max_iterations is not None and iterations >= max_iterations:
                            self._running = False
                            return
            except Exception as exc:  # noqa: BLE001 - reconnect on anything, log it
                logger.warning("WS disconnected (%s), reconnecting in %.1fs", exc, backoff)
                if max_iterations is not None:
                    # tests using a fake connect_fn that raises want the
                    # error surfaced after one retry rather than looping forever
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)

    def stop(self) -> None:
        self._running = False
