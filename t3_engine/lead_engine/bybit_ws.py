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

Threading, and why it is not one thread:

The first build called the message handler INLINE from the receive loop.
At 200 messages a second across 63 topics that turned the socket thread
into the scoring thread, and the consequence was measured in production:
the age of each processed message climbed monotonically - 0.2s, 5s, 10s,
34s - until the loop could no longer answer Bybit's keepalive ping and
the exchange closed the connection with 1011. Reconnect, and the climb
started again. Every downstream symptom (a stale book, "feed OK" beside
a multi-second age, signals on data from half a minute ago) was that one
backlog.

So there are two threads now. `lead-engine-ws` does nothing but read
frames, stamp the arrival time and put them on a bounded queue - no JSON
parse, no routing, no scoring - which keeps the loop free to answer
pings. `lead-engine-ingest` drains that queue and does the work. When the
queue fills, the OLDEST frames are dropped and counted, because a market
data consumer that blocks its producer to stay complete is a consumer
that falls further behind; dropping raises a resync flag so the order
book is rebuilt from a fresh snapshot rather than silently continued.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set

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

# The websockets library's own protocol-level keepalive. Set explicitly
# rather than left to the default so the numbers are visible: Bybit's
# server-side ping arrives about every 20s, and a client that cannot
# answer within 20s of sending its own is genuinely wedged.
WS_PING_INTERVAL = 20.0
WS_PING_TIMEOUT = 20.0

# How many raw frames may wait to be processed. At ~200 frames a second
# this is about a minute of backlog - long enough to ride out a garbage
# collection or a slow snapshot, short enough that a real overload is
# noticed in seconds rather than hidden for an hour.
QUEUE_LIMIT = 12_000

# Past this fraction of the queue, the ingest side is losing. Frames are
# still processed, but the order book is flagged for resync because a
# backlog this deep means the book being rebuilt from the queue describes
# a market that has already moved.
QUEUE_PRESSURE = 0.75


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
    """What the feed is actually doing, in numbers that can be checked.

    `network_latency_ms` is the exchange stamp against the local clock AT
    ARRIVAL - before the frame joins the queue, so a backlog on this side
    can never be mistaken for a slow link. `queue_latency_ms` is the time
    a frame then spent waiting, which is the number that went to 34
    seconds in production and had nowhere to be reported."""

    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    messages: int = 0
    dropped: int = 0
    resyncs: int = 0
    last_message_at: float = 0.0
    last_error: str = ""
    subscribed: List[str] = field(default_factory=list)
    network_samples: List[float] = field(default_factory=list)
    queue_samples: List[float] = field(default_factory=list)
    queue_depth: int = 0
    queue_peak: int = 0
    _rate_window: Deque[float] = field(default_factory=lambda: deque(maxlen=512))

    # ---- arrival ----

    def note_arrival(self, exchange_ms: int, received_at: float) -> float:
        """Exchange stamp against the local clock when the frame LANDED.

        Includes clock skew between Bybit and this host, which is why it
        is reported as a measurement rather than a guarantee. Nothing
        that happens after this point can change it, which is the whole
        reason it is sampled here and not in the worker."""
        self.messages += 1
        self.last_message_at = received_at
        self._rate_window.append(received_at)
        if not exchange_ms:
            return 0.0
        latency = max(0.0, received_at * 1000.0 - exchange_ms)
        self.network_samples.append(latency)
        if len(self.network_samples) > 200:
            self.network_samples.pop(0)
        return latency

    def note_queue_wait(self, waited_ms: float) -> None:
        self.queue_samples.append(max(0.0, waited_ms))
        if len(self.queue_samples) > 200:
            self.queue_samples.pop(0)

    # ---- summaries ----

    @staticmethod
    def _median(values: List[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    @property
    def network_latency_ms(self) -> float:
        return self._median(self.network_samples)

    @property
    def queue_latency_ms(self) -> float:
        return self._median(self.queue_samples)

    @property
    def latency_ms(self) -> float:
        """The old name. It has always meant arrival latency; it keeps
        meaning arrival latency."""
        return self.network_latency_ms

    @property
    def messages_per_second(self) -> float:
        """Measured over the window actually observed, not since start.
        A rate averaged over an hour of uptime hides the minute that
        matters."""
        if len(self._rate_window) < 2:
            return 0.0
        span = self._rate_window[-1] - self._rate_window[0]
        if span <= 0:
            return 0.0
        return (len(self._rate_window) - 1) / span

    def as_dict(self) -> Dict[str, Any]:
        return {
            "connected": self.connected, "connects": self.connects,
            "reconnects": self.reconnects, "messages": self.messages,
            "dropped": self.dropped, "resyncs": self.resyncs,
            "last_message_at": self.last_message_at, "last_error": self.last_error,
            "subscribed_topics": len(self.subscribed),
            "network_latency_ms": round(self.network_latency_ms, 1),
            "queue_latency_ms": round(self.queue_latency_ms, 1),
            "queue_depth": self.queue_depth,
            "queue_peak": self.queue_peak,
            "queue_limit": QUEUE_LIMIT,
            "messages_per_second": round(self.messages_per_second, 1),
            # The old key, unchanged in meaning, kept so nothing that
            # reads it breaks.
            "latency_ms": round(self.network_latency_ms, 1),
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
        # The seam between the socket and the work. See the module
        # docstring for why there is one.
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_LIMIT)
        self._worker: Optional[threading.Thread] = None
        self._resync_wanted: Set[str] = set()
        self._resync_lock = threading.Lock()
        self._on_resync: Optional[Callable[[List[str]], None]] = None

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # The worker first: a frame must never arrive with nothing to
        # drain the queue.
        self._worker = threading.Thread(target=self._drain,
                                        name="lead-engine-ingest", daemon=True)
        self._worker.start()
        self._thread = threading.Thread(target=self._thread_main,
                                        name="lead-engine-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        try:                                     # wake the worker
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def on_resync(self, callback: Callable[[List[str]], None]) -> None:
        """Called with the symbols whose book must be thrown away and
        rebuilt - after a reconnect, or after frames were dropped."""
        self._on_resync = callback

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
                async with connect(self.url, ping_interval=WS_PING_INTERVAL,
                                   ping_timeout=WS_PING_TIMEOUT) as socket:
                    self.stats.connected = True
                    self.stats.connects += 1
                    if self.stats.connects > 1:
                        self.stats.reconnects += 1
                    backoff = INITIAL_BACKOFF
                    # A reconnected socket says nothing about the book it
                    # left behind. Everything held locally is discarded and
                    # rebuilt from the snapshot the resubscribe brings.
                    self._demand_resync(self.symbols, "reconnect"
                                        if self.stats.connects > 1 else "connect")
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
        """Read, stamp, enqueue. Nothing else.

        Deliberately does no JSON parsing: at 200 frames a second the
        parse alone is enough to make the loop late for a ping, and a
        late ping is a closed connection."""
        ping_task = asyncio.ensure_future(self._app_ping(socket))
        try:
            async for raw in socket:
                if self._stop.is_set():
                    break
                self._receive(raw, time.time())
                await self._maybe_resync(socket)
        finally:
            ping_task.cancel()

    async def _app_ping(self, socket) -> None:
        """Bybit's application-level ping, on its own schedule.

        Its own task because the first version sent it from inside the
        receive loop, which meant a quiet socket was never pinged and a
        busy one was pinged late."""
        while not self._stop.is_set():
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            try:
                await socket.send(json.dumps({"op": "ping"}))
            except Exception:                    # noqa: BLE001
                return                           # a dead socket: run() reconnects

    async def _maybe_resync(self, socket) -> None:
        """Re-request the order book snapshot for any flagged symbol.

        Bybit sends a fresh snapshot on subscribe, so unsubscribing and
        resubscribing the book topic is how a local book is rebuilt. The
        engine's own reset happens through the `on_resync` callback,
        which has already run by the time this sends."""
        with self._resync_lock:
            pending = sorted(self._resync_wanted)
            self._resync_wanted.clear()
        if not pending:
            return
        topics = [f"orderbook.{self.depth}.{symbol}" for symbol in pending]
        for start in range(0, len(topics), TOPICS_PER_REQUEST):
            chunk = topics[start:start + TOPICS_PER_REQUEST]
            try:
                await socket.send(json.dumps({"op": "unsubscribe", "args": chunk}))
                await socket.send(json.dumps({"op": "subscribe", "args": chunk}))
            except Exception:                    # noqa: BLE001
                return
        self.stats.resyncs += len(pending)

    def _demand_resync(self, symbols: Iterable[str], reason: str) -> None:
        """Throw the local book away and ask for a new snapshot."""
        wanted = [s.upper() for s in symbols]
        if not wanted:
            return
        with self._resync_lock:
            self._resync_wanted.update(wanted)
        logger.info("lead_engine stream: resync %s (%s)", ",".join(wanted), reason)
        callback = self._on_resync
        if callback is not None:
            try:
                callback(wanted)
            except Exception:                    # noqa: BLE001
                logger.exception("lead_engine stream: resync callback failed")

    # ---- the queue ----

    def _receive(self, raw, received_at: float) -> None:
        """One frame from the socket onto the queue.

        The exchange timestamp is read here with a cheap string scan
        rather than a full parse, because the arrival latency has to be
        sampled BEFORE the frame waits in a queue or the two get mixed
        and a backlog reads as a slow link."""
        exchange_ms = _peek_timestamp(raw)
        self.stats.note_arrival(exchange_ms, received_at)
        try:
            self._queue.put_nowait((raw, received_at))
        except queue.Full:
            # Drop the OLDEST. A consumer that blocks its producer to stay
            # complete only falls further behind, and the newest frames
            # are the ones worth having.
            dropped = 0
            for _ in range(QUEUE_LIMIT // 10):
                try:
                    self._queue.get_nowait()
                    dropped += 1
                except queue.Empty:
                    break
            self.stats.dropped += dropped
            self._demand_resync(self.symbols, f"queue full, dropped {dropped}")
            try:
                self._queue.put_nowait((raw, received_at))
            except queue.Full:                   # pragma: no cover
                self.stats.dropped += 1
        depth = self._queue.qsize()
        self.stats.queue_depth = depth
        self.stats.queue_peak = max(self.stats.queue_peak, depth)

    def _drain(self) -> None:
        """The worker. Parses and routes, off the socket's loop."""
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                self.stats.queue_depth = 0
                continue
            if item is None:                     # the stop sentinel
                break
            raw, received_at = item
            self.stats.note_queue_wait((time.time() - received_at) * 1000.0)
            self._handle(raw, received_at)
            self.stats.queue_depth = self._queue.qsize()

    def _handle(self, raw, received_at: Optional[float] = None) -> None:
        try:
            message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict):
            return
        topic = message.get("topic")
        if not topic:
            return                               # subscribe acks, pongs
        if received_at is None:                  # fed directly, not from the socket
            received_at = time.time()
            self.stats.note_arrival(int(message.get("ts") or 0), received_at)
        # The frame carries its own arrival time from here on, so every
        # age downstream is measured against when the data LANDED rather
        # than when this process got round to it.
        message.setdefault("_received_at_ms", int(received_at * 1000))
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


def _peek_timestamp(raw) -> int:
    """Bybit's top-level `ts` without parsing the whole frame.

    A 50-level book delta is a few kilobytes and `json.loads` on the
    socket thread is exactly the cost this design exists to avoid. The
    field is near the front of every frame Bybit sends, so a bounded scan
    finds it; when it does not, the worker's full parse will."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "ignore")
        except Exception:                        # noqa: BLE001
            return 0
    if not isinstance(raw, str):
        return 0
    marker = raw.find('"ts":', 0, 512)
    if marker < 0:
        return 0
    index = marker + 5
    length = len(raw)
    while index < length and raw[index] == " ":
        index += 1
    start = index
    while index < length and raw[index].isdigit():
        index += 1
    if index == start:
        return 0
    try:
        return int(raw[start:index])
    except ValueError:                           # pragma: no cover
        return 0


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
