"""The Lead Engine's own Bybit public WebSocket client.

Bybit only. There is no Binance path here, no fallback to one, and none
is to be added: Binance is blocked in this deployment (the project moved
off it after a live IP ban), and a fallback that cannot connect is not
resilience, it is a second way to fail.

Why this is not the project's existing `BybitFuturesWebSocketClient`: that
client subscribes to publicTrade and hands raw trade dicts to one
callback, which is exactly what the older pipeline needs. This engine
needs five topic families, per-topic routing, a sequence-checked order
book, reconnect accounting and a subscribe that survives Bybit's
arguments-per-request limit. Retrofitting all of that into the shared
client would make the older pipeline depend on changes made for this one
- the coupling the specification exists to prevent - so this is a second,
separate client, and the two do not import each other.

Isolation: it runs its own asyncio loop on its own thread. The web
process's event loop is never used, so a stall here cannot stall the
application, and an exception here cannot unwind into a request handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Bybit refuses an over-long subscribe; ten topics per request is the
# documented safe size and costs nothing to respect.
TOPICS_PER_REQUEST = 10

# Reconnect backoff. Capped so a long outage does not turn into an hour
# between attempts.
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0

# Bybit closes an idle connection; a ping well inside that keeps it.
PING_INTERVAL_SECONDS = 20.0


def topics_for(symbols: Iterable[str], kline_intervals: Iterable[str],
               depth: int = 50) -> List[str]:
    """Every topic this engine subscribes to, in one place.

    Listed as data so the documentation (docs/lead-engine/BYBIT_STREAMS.md)
    and the client cannot drift apart, and so a test can assert the exact
    set rather than a substring of a log line."""
    out: List[str] = []
    for symbol in symbols:
        symbol = symbol.upper()
        out.append(f"orderbook.{depth}.{symbol}")
        out.append(f"publicTrade.{symbol}")
        out.append(f"tickers.{symbol}")
        out.append(f"allLiquidation.{symbol}")
        for interval in kline_intervals:
            out.append(f"kline.{interval}.{symbol}")
    return out


@dataclass
class StreamStats:
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    messages: int = 0
    last_message_at: float = 0.0
    last_error: str = ""
    subscribed: List[str] = field(default_factory=list)
    latency_samples: List[float] = field(default_factory=list)

    def note_latency(self, exchange_ms: int) -> float:
        """Exchange stamp against local clock. Includes clock skew, and is
        reported as a measurement rather than a guarantee - see health.py."""
        if not exchange_ms:
            return 0.0
        latency = max(0.0, time.time() * 1000.0 - exchange_ms)
        self.latency_samples.append(latency)
        if len(self.latency_samples) > 200:
            self.latency_samples.pop(0)
        return latency

    @property
    def latency_ms(self) -> float:
        if not self.latency_samples:
            return 0.0
        ordered = sorted(self.latency_samples)
        return ordered[len(ordered) // 2]          # median, not mean

    def as_dict(self) -> Dict[str, Any]:
        return {
            "connected": self.connected, "connects": self.connects,
            "reconnects": self.reconnects, "messages": self.messages,
            "last_message_at": self.last_message_at, "last_error": self.last_error,
            "subscribed_topics": len(self.subscribed),
            "latency_ms": round(self.latency_ms, 1),
        }


class BybitLeadStream:
    """Connects, subscribes, routes. Knows nothing about what the
    messages mean - that is the feature modules' job."""

    def __init__(self, symbols: List[str], kline_intervals: List[str],
                 on_message: Callable[[str, Dict[str, Any]], None],
                 url: str, depth: int = 50, connect_fn=None) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.kline_intervals = list(kline_intervals)
        self.on_message = on_message
        self.url = url
        self.depth = depth
        self.stats = StreamStats()
        self._connect_fn = connect_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main,
                                        name="lead-engine-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run())
        except Exception:                        # noqa: BLE001 - the whole
            logger.exception("lead_engine stream thread ended")   # point of
        finally:                                 # the thread is to contain this
            self.stats.connected = False
            try:
                loop.close()
            finally:
                self._loop = None

    # ---- the socket ----

    def _resolve_connect(self):
        if self._connect_fn is not None:
            return self._connect_fn
        import websockets           # imported lazily: only needed when live
        return websockets.connect

    async def run(self) -> None:
        backoff = INITIAL_BACKOFF
        connect = self._resolve_connect()
        while not self._stop.is_set():
            try:
                async with connect(self.url) as socket:
                    self.stats.connected = True
                    self.stats.connects += 1
                    if self.stats.connects > 1:
                        self.stats.reconnects += 1
                    backoff = INITIAL_BACKOFF
                    await self._subscribe(socket)
                    await self._consume(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:             # noqa: BLE001
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("lead_engine stream: %s", self.stats.last_error)
            finally:
                self.stats.connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)

    async def _subscribe(self, socket) -> None:
        topics = topics_for(self.symbols, self.kline_intervals, self.depth)
        self.stats.subscribed = topics
        for start in range(0, len(topics), TOPICS_PER_REQUEST):
            chunk = topics[start:start + TOPICS_PER_REQUEST]
            await socket.send(json.dumps({"op": "subscribe", "args": chunk}))

    async def _consume(self, socket) -> None:
        last_ping = time.time()
        async for raw in socket:
            if self._stop.is_set():
                break
            now = time.time()
            if now - last_ping > PING_INTERVAL_SECONDS:
                try:
                    await socket.send(json.dumps({"op": "ping"}))
                except Exception:                # noqa: BLE001
                    break                        # a dead socket: reconnect
                last_ping = now
            self._handle(raw)

    def _handle(self, raw) -> None:
        try:
            message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict):
            return
        topic = message.get("topic")
        if not topic:
            return                               # subscribe acks, pongs
        self.stats.messages += 1
        self.stats.last_message_at = time.time()
        self.stats.note_latency(int(message.get("ts") or 0))
        try:
            self.on_message(str(topic), message)
        except Exception:                        # noqa: BLE001 - one bad
            logger.exception("lead_engine: handler failed for %s", topic)  # frame
            # is never a reason to drop the connection.

    def feed(self, message: Dict[str, Any]) -> None:
        """Push one message in as though it had arrived on the socket.

        The seam replay and the tests use. It is the SAME path a live
        message takes - `_handle` - so nothing can be true of a replayed
        message that is not true of a live one."""
        self._handle(message)


def parse_topic(topic: str) -> Dict[str, str]:
    """`orderbook.50.INJUSDT` -> {kind, symbol, detail}.

    Bybit's topics are dot-separated with the symbol last and an optional
    middle field (depth, kline interval), so the parse is positional from
    the ends rather than a table of formats."""
    parts = str(topic).split(".")
    if len(parts) < 2:
        return {"kind": str(topic), "symbol": "", "detail": ""}
    kind = parts[0]
    symbol = parts[-1].upper()
    detail = parts[1] if len(parts) > 2 else ""
    return {"kind": kind, "symbol": symbol, "detail": detail}
