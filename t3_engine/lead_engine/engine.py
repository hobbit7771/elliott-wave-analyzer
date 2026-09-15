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
from t3_engine.lead_engine.smc_engine import candle_from_kline
from t3_engine.lead_engine.storage import Recorder, Storage
from t3_engine.lead_engine.state import SymbolState
from t3_engine.lead_engine.trade_flow import Trade

logger = logging.getLogger(__name__)

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
                depth=self.config.orderbook_depth,
                connect_fn=self._connect_fn,
            )
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
                           "kline_intervals": list(self.config.kline_intervals)},
                "note": "lead engine start",
            })
            self.started_at = time.time()
            self.start_error = ""
            logger.info("lead_engine: started on %d symbols", len(self.config.symbols))
            return True
        except Exception as exc:                 # noqa: BLE001 - see docstring
            self.start_error = f"{type(exc).__name__}: {exc}"
            logger.exception("lead_engine: failed to start; the rest of the app is unaffected")
            return False

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
            state.health.latency_ms = self.stream.stats.latency_ms
            state.health.reconnects = self.stream.stats.reconnects

        kind = parsed["kind"]
        if kind == "orderbook":
            state.on_orderbook(message)
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

        state.health.processing_ms = (time.perf_counter() - began) * 1000.0

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
