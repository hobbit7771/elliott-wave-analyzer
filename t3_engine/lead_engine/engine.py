"""LeadEngine - the whole module behind five methods.

This is the boundary the specification asks for. Anything outside this
package that wants something from the Lead Engine calls one of:

    LeadEngine.subscribe(symbol)
    LeadEngine.get_state(symbol)
    LeadEngine.get_signal(symbol)
    LeadEngine.get_pressure(symbol)
    LeadEngine.status()

and gets plain dictionaries back. The older system is never told how an
OBI, a CVD, a microprice or a break probability is computed, and it holds
no reference to any object inside this package. That is what makes the
two independently replaceable, and it is also what makes the failure
isolation real rather than aspirational: `start()` catches everything, so
a Lead Engine that cannot start leaves an application that runs without
it.

The flag is checked here and only here. `LEAD_ENGINE_ENABLED=false` means
`start()` does nothing, no thread exists, no socket opens, no polling
happens, and every accessor returns a disabled marker. There is no
half-on state.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from t3_engine.lead_engine import bus as bus_module
from t3_engine.lead_engine import config as config_module
from t3_engine.lead_engine.btc_leadlag import BtcLeadLag
from t3_engine.lead_engine.bybit_ws import BybitLeadStream, parse_topic
from t3_engine.lead_engine.config import BTC_SYMBOL, LeadEngineConfig
from t3_engine.lead_engine.liquidation_engine import liquidation_from_message
from t3_engine.lead_engine.oi_engine import OpenInterestPoller
from t3_engine.lead_engine import candles_rest
from t3_engine.lead_engine.smc_engine import Candle as SmcCandle
from t3_engine.lead_engine.smc_engine import candle_from_kline
from t3_engine.lead_engine.storage import Recorder, Storage
from t3_engine.lead_engine.state import STRUCTURE_INTERVAL, SymbolState
from t3_engine.lead_engine.trade_flow import Trade

# The structure series is subscribed as Bybit's "1"; the REST helper
# spells the same interval "1m". Two vocabularies for one thing, so the
# translation lives here rather than being written out at the call site.
STRUCTURE_INTERVAL_LABEL = "1m"

# How many closed minute bars to seed. Four hours: long enough that the
# series has certainly turned, short enough to be one request per symbol.
BACKFILL_BARS = 240

# The backfill competes with the ingest thread for the GIL, so it waits
# for the socket to settle and yields between symbols. Both are small:
# the whole seed still completes inside fifteen seconds, which is one
# fine bar, and the engine has its levels long before it could have built
# a single swing on its own.
BACKFILL_START_DELAY_SECONDS = 2.0
BACKFILL_GAP_SECONDS = 0.5

logger = logging.getLogger(__name__)

# How long to wait before asking Bybit for the same symbol's book again.
# A resubscribe is not free: it costs a round trip and produces its own
# discontinuity, so asking on every gap turns one lost frame into a loop.
RESYNC_DEBOUNCE_SECONDS = 5.0

DISABLED_PAYLOAD: Dict[str, Any] = {
    "enabled": False,
    "detail": (
        "The Market Lead Engine is switched off. Set LEAD_ENGINE_ENABLED=true "
        "to run it; while it is off no socket is opened, no thread is started "
        "and no polling happens."
    ),
}


class LeadEngine:
    def __init__(self, config: Optional[LeadEngineConfig] = None,
                 connect_fn=None,
                 oi_fetcher: Optional[Callable[[str, str], Optional[Dict[str, float]]]] = None,
                 storage=None) -> None:
        self.config = config or LeadEngineConfig.from_env()
        self.bus = bus_module.EventBus()
        self.lead_lag = BtcLeadLag(BTC_SYMBOL)
        self.states: Dict[str, SymbolState] = {}
        self.started_at: Optional[float] = None
        self.start_error = ""
        self._lock = threading.RLock()
        self._resync_asked: Dict[str, float] = {}
        self._connect_fn = connect_fn
        self._oi_fetcher = oi_fetcher
        self._storage = storage
        self.stream: Optional[BybitLeadStream] = None
        self.oi_poller: Optional[OpenInterestPoller] = None
        self.storage: Storage = storage or Storage()
        self.recorder: Optional[Recorder] = None
        for symbol in self.config.symbols:
            self._ensure(symbol)

    # ---- lifecycle ----

    def _btc_state(self) -> Optional[SymbolState]:
        """The reference instrument's own state, for the BTC lead layer.

        Passed in rather than reached for, so `SymbolState` still knows
        nothing about the engine that owns it and stays testable alone."""
        return self.states.get(BTC_SYMBOL)

    def _ensure(self, symbol: str) -> SymbolState:
        symbol = symbol.upper()
        with self._lock:
            state = self.states.get(symbol)
            if state is None:
                state = SymbolState(symbol, self.config, self.lead_lag,
                                    self.config.thresholds)
                self.states[symbol] = state
            return state

    def start(self) -> bool:
        """Bring the engine up. Never raises.

        Returns whether it started. A False here must leave the rest of
        the application untouched - that is the isolation requirement, and
        the try/except is how it is kept rather than hoped for."""
        if not self.config.enabled:
            logger.info("lead_engine: disabled by configuration; nothing started")
            return False
        if self.stream is not None and self.stream.running():
            return True
        try:
            self.stream = BybitLeadStream(
                symbols=self.config.symbols,
                kline_intervals=self.config.kline_intervals,
                on_message=self.handle_message,
                url=self.config.ws_url,
                # The TOPIC depth, which sets Bybit's push rate. The
                # analysis depth is the book's own, and they are not the
                # same number - see config.ORDERBOOK_TOPIC_DEPTH.
                depth=self.config.orderbook_topic_depth,
                connect_fn=self._connect_fn,
            )
            # A book that desynced used to stay dead until the next
            # reconnect, because nothing asked Bybit for another
            # snapshot. The stream now tells the engine which books to
            # throw away, and asks the exchange to resend them.
            self.stream.on_resync(self._reset_books)
            self.stream.start()
            self.oi_poller = OpenInterestPoller(
                symbols=self.config.symbols,
                base_url=self.config.rest_base,
                on_reading=self._on_open_interest,
                interval_seconds=self.config.oi_poll_seconds,
                fetcher=self._oi_fetcher,
            )
            self.oi_poller.start()
            # Persist what the engine sees whether or not a browser is
            # open. Without this the tables only fill when someone looks
            # at the tab, which makes a 24/7 monitor into a dashboard.
            self.recorder = Recorder(self, self.storage)
            self.recorder.start()
            self.storage.record_session({
                "started_at": int(time.time() * 1000),
                "symbols": ",".join(self.config.symbols),
                "config": {"weights": self.config.weights.as_dict(),
                           "orderbook_depth": self.config.orderbook_depth,
                           "orderbook_topic_depth": self.config.orderbook_topic_depth,
                           "kline_intervals": list(self.config.kline_intervals)},
                "note": "lead engine start",
            })
            # History, before the first live bar closes. See
            # _backfill_structure: without it the engine spends its first
            # ten minutes unable to name a level, and on a host that
            # restarts every fifteen that is most of its life.
            threading.Thread(target=self._backfill_structure,
                             name="lead-engine-backfill", daemon=True).start()
            self.started_at = time.time()
            self.start_error = ""
            logger.info("lead_engine: started on %d symbols", len(self.config.symbols))
            return True
        except Exception as exc:                 # noqa: BLE001 - see docstring
            self.start_error = f"{type(exc).__name__}: {exc}"
            logger.exception("lead_engine: failed to start; the rest of the app is unaffected")
            return False

    def _backfill_structure(self) -> None:
        """Seed each symbol's minute structure from Bybit REST.

        THE PROBLEM THIS SOLVES. The engine used to start with no history
        at all and build it from the trade stream, at fifteen seconds a
        bar. Three minutes in it holds twelve bars - and if those twelve
        trended, a fractal detector finds no swing in them, because no bar
        is higher than the two that follow it. No swing, no level, no
        PRE_BREAK, no direction: the panel reports "no resistance
        identified above the current price" while the order book is
        visibly trading. Ten minutes of history fixes it, and the free
        Render plan stops the process after fifteen.

        A minute of Bybit REST gives what an hour of waiting would.

        Runs on its own thread and never raises: an engine that cannot
        reach the REST endpoint is an engine with less history, not a
        dead one - it simply builds the bars itself as it always did."""
        interval = STRUCTURE_INTERVAL_LABEL
        # Let the socket settle before competing with it. Measured: the
        # seven REST calls and the JSON parse of 240 candles each hold the
        # GIL in bursts, and the first heartbeat after a start reported
        # book_age p95 of 1012ms against 636ms once the run settled. The
        # data is muted as stale while that lasts, so nothing acts on it -
        # but a second of slack here costs nothing and the tail is real.
        time.sleep(BACKFILL_START_DELAY_SECONDS)
        for symbol in list(self.config.symbols):
            try:
                rows = candles_rest.fetch_candles(
                    symbol, interval, limit=BACKFILL_BARS,
                    base_url=self.config.rest_base)
            except Exception:                    # noqa: BLE001 - see docstring
                logger.debug("lead_engine: %s structure not backfilled",
                             symbol, exc_info=True)
                continue
            if not rows:
                continue
            state = self.states.get(symbol) or self._ensure(symbol)
            seeded = 0
            for row in rows:
                # The forming bar is skipped: it is not a closed bar and
                # the socket is about to send it anyway.
                if not row.get("closed", True):
                    continue
                try:
                    state.on_kline(STRUCTURE_INTERVAL, SmcCandle(
                        start_ms=int(row["time"]) * 1000,
                        open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0), closed=True))
                    seeded += 1
                except (KeyError, TypeError, ValueError):
                    continue
            if seeded:
                logger.info("lead_engine: %s seeded with %d closed %s bars",
                            symbol, seeded, interval)
            # Hand the ingest thread the GIL between symbols rather than
            # running seven parses back to back.
            time.sleep(BACKFILL_GAP_SECONDS)

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
        if self.oi_poller is not None:
            self.oi_poller.stop()
        if self.recorder is not None:
            # Flushes what is buffered on the way out, so a clean shutdown
            # does not throw away the last interval's rows.
            self.recorder.stop()
        self.started_at = None

    def running(self) -> bool:
        return bool(self.config.enabled and self.stream is not None and self.stream.running())

    # ---- ingest ----

    def handle_message(self, topic: str, message: Dict[str, Any]) -> None:
        """Route one Bybit frame. Called from the stream thread."""
        began = time.perf_counter()
        parsed = parse_topic(topic)
        symbol = parsed["symbol"]
        if not symbol:
            return
        state = self._ensure(symbol)
        state.health.ws_connected = True
        state.health.messages += 1
        if self.stream is not None:
            stats = self.stream.stats
            state.health.reconnects = stats.reconnects
            state.health.dropped_messages = stats.dropped
            if not state.health.last_book_receive_ms:
                # Until this symbol's own book has landed, the stream's
                # median is the only latency figure there is. Once it has,
                # `on_orderbook` sets the per-frame value, which is the
                # one that belongs to this instrument.
                state.health.network_latency_ms = stats.network_latency_ms

        kind = parsed["kind"]
        if kind == "orderbook":
            was_synced = state.book.synced
            state.on_orderbook(message)
            if was_synced and not state.book.synced:
                # The sequence broke, or the book crossed. Neither repairs
                # itself: Bybit only sends a snapshot on subscribe, so a
                # desynced book stays desynced until one is asked for.
                self._request_resync(symbol, state.book.desync_reason)
            self.bus.publish(bus_module.TOPIC_ORDERBOOK,
                             {"symbol": symbol, "synced": state.book.synced})
        elif kind == "publicTrade":
            self._on_trades(state, message)
        elif kind == "tickers":
            data = message.get("data") or {}
            if isinstance(data, dict):
                state.on_ticker(data, int(message.get("ts") or 0))
                self.bus.publish(bus_module.TOPIC_TICKER, {"symbol": symbol})
        elif kind == "allLiquidation":
            self._on_liquidations(state, message)
        elif kind == "kline":
            self._on_klines(state, parsed["detail"], message)

        state.health.processing_latency_ms = (time.perf_counter() - began) * 1000.0
        state.health.last_process_ms = int(time.time() * 1000)

    def _reset_books(self, symbols: List[str]) -> None:
        """Called by the stream when a book must be rebuilt from scratch."""
        for symbol in symbols:
            state = self.states.get(str(symbol).upper())
            if state is None:
                continue
            state.book.reset("stream resync")
            state.health.orderbook_synced = False

    def _request_resync(self, symbol: str, reason: str) -> None:
        """Ask the stream for a fresh snapshot, at most once every
        RESYNC_DEBOUNCE_SECONDS per symbol.

        Debounced because a resubscribe under load produces its own
        discontinuity: the first version asked on every gap, so one lost
        frame could start a loop where each resync guaranteed the next.
        Between requests the book stays desynced and its signals stay
        muted, which is the correct state to be in - it is not serving
        numbers from a book it cannot trust, it is waiting for a good
        one."""
        stream = self.stream
        if stream is None:
            return
        now = time.time()
        last = self._resync_asked.get(symbol, 0.0)
        if now - last < RESYNC_DEBOUNCE_SECONDS:
            return
        self._resync_asked[symbol] = now
        try:
            stream._demand_resync([symbol], reason or "desync")
        except Exception:                        # noqa: BLE001 - a failed
            logger.debug("lead_engine: resync request failed for %s",  # resync
                         symbol, exc_info=True)  # is retried on the next gap

    def _on_trades(self, state: SymbolState, message: Dict[str, Any]) -> None:
        for item in (message.get("data") or []):
            try:
                trade = Trade(
                    timestamp_ms=int(item.get("T") or message.get("ts") or 0),
                    price=float(item.get("p")),
                    quantity=float(item.get("v")),
                    # Bybit's `S` is the TAKER's side, so "Buy" is someone
                    # lifting the offer. No inversion here - unlike
                    # Binance's aggTrade flag, which means the opposite.
                    is_taker_buy=str(item.get("S", "")).strip().lower().startswith("b"),
                )
            except (TypeError, ValueError):
                continue
            if trade.quantity <= 0 or trade.price <= 0:
                continue
            state.on_trade(trade)
        self.bus.publish(bus_module.TOPIC_TRADE,
                         {"symbol": state.symbol, "count": len(message.get("data") or [])})

    def _on_liquidations(self, state: SymbolState, message: Dict[str, Any]) -> None:
        events = 0
        for item in (message.get("data") or []):
            event = liquidation_from_message(item, state.symbol)
            if event is None:
                continue
            state.on_liquidation(event)
            events += 1
        if events:
            self.bus.publish(bus_module.TOPIC_LIQUIDATION,
                             {"symbol": state.symbol, "events": events,
                              "state": state.liquidations.state()})

    def _on_klines(self, state: SymbolState, interval: str, message: Dict[str, Any]) -> None:
        for item in (message.get("data") or []):
            candle = candle_from_kline(item, interval)
            if candle is None or candle.start_ms <= 0:
                continue
            state.on_kline(interval or "1", candle)
        self.bus.publish(bus_module.TOPIC_KLINE,
                         {"symbol": state.symbol, "interval": interval})

    def _on_open_interest(self, symbol: str, value: float, stamp_ms: int) -> None:
        state = self._ensure(symbol)
        state.on_open_interest(value, stamp_ms)
        self.bus.publish(bus_module.TOPIC_OPEN_INTEREST,
                         {"symbol": symbol, "open_interest": value})

    # ---- the public surface ----

    def subscribe(self, symbol: str) -> Dict[str, Any]:
        """Make sure this symbol is being tracked.

        Adding a symbol after the socket is up needs a resubscribe, which
        this does by restarting the stream. Reported honestly rather than
        pretending the new symbol is live immediately."""
        if not self.config.enabled:
            return dict(DISABLED_PAYLOAD)
        symbol = symbol.upper()
        if symbol in self.states and symbol in self.config.symbols:
            return {"enabled": True, "symbol": symbol, "already_subscribed": True}
        self._ensure(symbol)
        if symbol not in self.config.symbols:
            self.config.symbols.append(symbol)
        restarted = False
        if self.stream is not None and self.stream.running():
            self.stream.stop()
            self.stream = None
            restarted = self.start()
        return {"enabled": True, "symbol": symbol, "already_subscribed": False,
                "stream_restarted": restarted}

    def get_state(self, symbol: str, force: bool = False) -> Dict[str, Any]:
        if not self.config.enabled:
            return dict(DISABLED_PAYLOAD)
        symbol = symbol.upper()
        if symbol not in self.states:
            return {"enabled": True, "symbol": symbol, "tracked": False,
                    "detail": "not subscribed; call subscribe() first"}
        frame = self.states[symbol].snapshot(force=force, btc_state=self._btc_state())
        self.bus.publish(bus_module.TOPIC_FEATURES, {"symbol": symbol})
        return {"enabled": True, "tracked": True, **frame}

    def get_signal(self, symbol: str) -> Dict[str, Any]:
        if not self.config.enabled:
            return dict(DISABLED_PAYLOAD)
        symbol = symbol.upper()
        if symbol not in self.states:
            return {"enabled": True, "symbol": symbol, "tracked": False}
        state = self.states[symbol]
        state.snapshot(btc_state=self._btc_state())
        return {"enabled": True, "tracked": True, "symbol": symbol,
                **state.signals.as_dict()}

    def get_pressure(self, symbol: str) -> Dict[str, Any]:
        if not self.config.enabled:
            return dict(DISABLED_PAYLOAD)
        symbol = symbol.upper()
        if symbol not in self.states:
            return {"enabled": True, "symbol": symbol, "tracked": False}
        frame = self.states[symbol].snapshot(btc_state=self._btc_state())
        return {"enabled": True, "tracked": True, "symbol": symbol,
                **frame["pressure"],
                "prebreak": frame["prebreak"]}

    def get_metrics(self, symbol: str) -> Dict[str, Any]:
        """The raw feature blocks, without the signal machinery."""
        frame = self.get_state(symbol)
        if not frame.get("tracked"):
            return frame
        return {key: frame[key] for key in
                ("symbol", "generated_at", "price", "orderbook", "microprice",
                 "trade_flow", "cvd", "liquidations", "open_interest",
                 "btc_lead", "smc", "elliott", "health")}

    def get_virtual_trades(self, symbol: str, limit: int = 50) -> Dict[str, Any]:
        """The virtual ledger for one symbol: summary plus the journal.

        `snapshot()` is called first so the signal - and therefore any
        intent it opens - is current. It does NOT fill anything: fills
        happen only on the ingest path, from a book that arrived after
        the signal. Reading this endpoint cannot create a trade."""
        if not self.config.enabled:
            return dict(DISABLED_PAYLOAD)
        symbol = symbol.upper()
        state = self.states.get(symbol)
        if state is None:
            return {"enabled": True, "symbol": symbol, "tracked": False,
                    "summary": None, "journal": []}
        state.snapshot(btc_state=self._btc_state())
        return {
            "enabled": True, "tracked": True, "symbol": symbol,
            "summary": state.ledger.summary(state.book.best_bid(),
                                            state.book.best_ask()),
            "journal": state.ledger.journal(max(1, min(int(limit), 500))),
        }

    def get_history(self, symbol: str, limit: int = 300) -> Dict[str, Any]:
        if not self.config.enabled:
            return dict(DISABLED_PAYLOAD)
        symbol = symbol.upper()
        state = self.states.get(symbol)
        if state is None:
            return {"enabled": True, "symbol": symbol, "tracked": False, "rows": []}
        rows = state.feature_history[-max(1, min(int(limit), 2000)):]
        return {"enabled": True, "tracked": True, "symbol": symbol, "rows": rows}

    def symbols(self) -> List[str]:
        return list(self.config.symbols)

    def status(self) -> Dict[str, Any]:
        """Everything the tab's header needs, for every symbol at once."""
        if not self.config.enabled:
            return {**DISABLED_PAYLOAD, "symbols": [], "stream": None}
        stream = self.stream.stats.as_dict() if self.stream is not None else None
        rows = []
        for symbol in self.config.symbols:
            state = self.states.get(symbol)
            if state is None:
                continue
            frame = state.snapshot(btc_state=self._btc_state())
            rows.append({
                "symbol": symbol,
                "price": frame["price"],
                "state": frame["signal"]["state"],
                "direction": frame["signal"]["direction"],
                "long_pressure": frame["pressure"]["long_pressure"],
                "short_pressure": frame["pressure"]["short_pressure"],
                "health": frame["health"]["status"],
                "signals_enabled": frame["health"]["signals_enabled"],
            })
        return {
            "enabled": True,
            "running": self.running(),
            "started_at": self.started_at,
            "start_error": self.start_error,
            "stream": stream,
            "oi_polling": bool(self.oi_poller and self.oi_poller.running()),
            "recorder": self.recorder.stats() if self.recorder else None,
            "storage": self.storage.stats(),
            "symbols": rows,
            "weights": self.config.weights.as_dict(),
            "bus_published": self.bus.published,
        }


# ---- the process-wide instance ----
# Created on first use, never at import: importing this package must not
# start anything, and a module-level instance would read the environment
# at import time and freeze the flag.

_engine: Optional[LeadEngine] = None
_engine_lock = threading.Lock()


def get_engine(config: Optional[LeadEngineConfig] = None) -> LeadEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = LeadEngine(config)
        return _engine


def reset_engine() -> None:
    """Drop the process-wide instance. For tests, and for a clean
    shutdown - nothing in production should need it."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.stop()
        _engine = None


def enabled() -> bool:
    return config_module.enabled()
