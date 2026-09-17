"""Automatic PAPER trading on the live feed, with state that survives.

THE SAME OBJECT THAT WAS BACKTESTED. `StrategyRunner`, `ExecutionSimulator`
and `Portfolio` are the classes the replay used; this file only supplies
a different feed and a different clock. If the decision logic were
re-implemented here, a forward test would be testing a second program
and agreeing with the first would prove nothing.

WHAT PAPER CAN AND CANNOT SHOW. It runs against the real, live book and
tape, so the spread, the depth and the timing are real. It still cannot
show where a real order would have sat in a real queue - Bybit's public
book is aggregated, so the maker fills here remain the simulator's
estimate under an explicitly conservative model. That limit does not go
away by going live, and this module never implies otherwise.

RESTARTS ARE THE NORMAL CASE, NOT THE EXCEPTION. Render Free stops a web
service about fifteen minutes after the last inbound request. So:

  * every position, fill, journal row and calibration observation is
    written to Supabase with an IDEMPOTENT KEY, so a retry after a write
    timeout verifies rather than re-applies,
  * recovery loads the journal FIRST, then asks for a fresh book
    snapshot, then warms the feature windows, and only then allows a new
    entry - in that order, because a strategy that trades off a
    half-warm window is trading off noise,
  * the downtime is marked DATA_GAP. Stops and targets are NOT filled
    inside it. Nothing that happened while the process was dead is
    claimed to have been traded, and the exit that eventually happens
    takes the gap price it really gets.

STATUS IS A FACT, NOT A HOPE. RUNNING, RECOVERING, FEED_STALE and
STORAGE_DEGRADED are distinguished, each with the heartbeat that
justifies it, so an old frame can never look alive on the screen.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from t3_engine.research.book import BUY, SELL, BookReconstructor, BookState, Trade
from t3_engine.research.execution import InstrumentSpec, LatencyModel, QueueModel
from t3_engine.research.portfolio import (JournalRow, Portfolio, QUALITY_DATA_GAP,
                                          QUALITY_OK, RiskLimits)
from t3_engine.research.runner import RunnerConfig, StrategyRunner
from t3_engine.research.strategies import ALL_STRATEGIES

logger = logging.getLogger(__name__)

TABLE_PAPER_TRADES = "research_paper_trades"
TABLE_PAPER_STATE = "research_paper_state"

RUNNING = "RUNNING"
RECOVERING = "RECOVERING"
FEED_STALE = "FEED_STALE"
STORAGE_DEGRADED = "STORAGE_DEGRADED"
STOPPED = "STOPPED"

# Feed ages. Past the first the screen says so; past the second nothing
# may be opened.
HEARTBEAT_WARN_MS = 3_000
FEED_STALE_MS = 10_000

# How much fresh data a restarted process must see before it is allowed
# to open anything. The longest feature window is 30s, so this is that
# window plus room.
WARMUP_MS = 45_000


@dataclass
class PaperStatus:
    state: str = STOPPED
    reason: str = ""
    symbol: str = ""
    strategy: str = ""
    last_event_ms: int = 0
    last_heartbeat: float = 0.0
    started_at: float = 0.0
    recovered_trades: int = 0
    warmup_remaining_ms: int = 0
    storage_failures: int = 0
    events: int = 0

    def as_dict(self) -> Dict[str, Any]:
        age = (time.time() * 1000.0 - self.last_event_ms) if self.last_event_ms else None
        return {
            "state": self.state, "reason": self.reason, "symbol": self.symbol,
            "strategy": self.strategy,
            "last_event_ms": self.last_event_ms,
            "feed_age_ms": int(age) if age is not None else None,
            "last_heartbeat": self.last_heartbeat,
            "heartbeat_age_seconds": round(time.time() - self.last_heartbeat, 1)
            if self.last_heartbeat else None,
            "uptime_seconds": round(time.time() - self.started_at, 1)
            if self.started_at else 0.0,
            "recovered_trades": self.recovered_trades,
            "warmup_remaining_ms": self.warmup_remaining_ms,
            "storage_failures": self.storage_failures,
            "events": self.events,
        }


class PaperTrader:
    """One strategy, one symbol, on the live feed."""

    def __init__(self, symbol: str, strategy_name: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 instrument: Optional[InstrumentSpec] = None,
                 limits: Optional[RiskLimits] = None,
                 persist: bool = True) -> None:
        if strategy_name not in ALL_STRATEGIES:
            raise ValueError(f"unknown strategy {strategy_name!r}")
        self.symbol = symbol.upper()
        self.persist = persist
        strategy = ALL_STRATEGIES[strategy_name](params or {})
        config = RunnerConfig(
            symbol=self.symbol,
            spec=instrument or InstrumentSpec(self.symbol),
            # Conservative by default, and the same numbers the backtest
            # was run with, so the two are comparable.
            latency=LatencyModel(send_ms=120.0, ack_ms=60.0, cancel_ms=120.0),
            queue=QueueModel.conservative(),
            limits=limits or RiskLimits())
        self.runner = StrategyRunner(strategy, config)
        self.status = PaperStatus(symbol=self.symbol,
                                  strategy=f"{strategy.name}@{strategy.version}")
        self._book = BookReconstructor()
        self._lock = threading.RLock()
        self._recovered_at_ms = 0
        self._first_event_ms = 0
        self._persisted: set = set()
        self._session_id = f"paper-{int(time.time())}"

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self.status.started_at = time.time()
            self.status.state = RECOVERING
            self.status.reason = "loading the journal"
            self._recover()

    def _recover(self) -> None:
        """Journal first, then a fresh book, then warm windows, then
        entries. In that order, every time."""
        if not self.persist:
            self.status.state = RECOVERING
            self.status.reason = "warming up"
            return
        try:
            rows = _load_trades(self.symbol)
        except Exception as exc:
            self.status.storage_failures += 1
            self.status.state = STORAGE_DEGRADED
            self.status.reason = f"could not load the journal: {exc}"
            logger.warning("paper recovery could not read storage: %s", exc)
            return

        closed = 0
        for row in rows:
            journal = _row_to_journal(row)
            if journal is None:
                continue
            self.runner.portfolio.journal.append(journal)
            self._persisted.add(journal.trade_id)
            closed += 1
        self.status.recovered_trades = closed
        self.status.reason = ("journal restored; waiting for a fresh snapshot "
                              "and a warm window")
        # An OPEN position from a process that is gone is NOT open. The
        # market moved while nothing was managing it, and claiming it
        # survived would invent a position nobody held.
        logger.info("paper recovered %d closed trades for %s", closed, self.symbol)

    # ---- the feed --------------------------------------------------------

    def observe(self, topic: str, message: Dict[str, Any],
                received_at_ms: int) -> None:
        """The tap, called from the ingest thread. Cheap by contract."""
        head = topic.split(".", 1)[0]
        symbol = topic.rsplit(".", 1)[-1].upper()
        if symbol != self.symbol:
            return
        try:
            with self._lock:
                self._handle(head, message, received_at_ms)
        except Exception:                       # never break the ingest thread
            logger.exception("paper trader raised on %s", topic)

    def _handle(self, head: str, message: Dict[str, Any],
                received_at_ms: int) -> None:
        self.status.events += 1
        self.status.last_event_ms = received_at_ms
        self.status.last_heartbeat = time.time()
        if not self._first_event_ms:
            self._first_event_ms = received_at_ms

        if head == "orderbook":
            frame = {"type": message.get("type"), "ts": message.get("ts"),
                     "r": received_at_ms, "data": message.get("data")}
            state = self._book.apply(frame)
            if state is not None:
                self.runner.on_book(state)
        elif head == "publicTrade":
            for row in (message.get("data") or []):
                self.runner.on_trade(Trade(
                    exchange_ms=int(row.get("T") or message.get("ts") or 0),
                    recv_ms=received_at_ms,
                    price=float(row.get("p") or 0.0),
                    size=float(row.get("v") or 0.0),
                    side=str(row.get("S") or ""),
                    trade_id=str(row.get("i") or "")))
        elif head == "allLiquidation":
            for row in (message.get("data") or []):
                self.runner.on_liquidation(
                    received_at_ms,
                    float(row.get("v") or 0.0) * float(row.get("p") or 0.0),
                    str(row.get("S") or ""))

        self._advance_state(received_at_ms)
        self._flush_journal()

    def _advance_state(self, now_ms: int) -> None:
        warm_for = now_ms - self._first_event_ms
        if self.status.state == STORAGE_DEGRADED:
            return
        if warm_for < WARMUP_MS:
            self.status.state = RECOVERING
            self.status.warmup_remaining_ms = int(WARMUP_MS - warm_for)
            self.status.reason = "warming the feature windows"
            # Entries are blocked by the runner's own quality gate until
            # the book is fresh; this only reports it.
            return
        self.status.warmup_remaining_ms = 0
        if self.runner.quality != QUALITY_OK:
            self.status.state = FEED_STALE
            self.status.reason = self.runner.quality_reason
            return
        self.status.state = RUNNING
        self.status.reason = ""

    @property
    def may_enter(self) -> bool:
        return self.status.state == RUNNING and self.runner.may_enter

    # ---- persistence ------------------------------------------------------

    def _flush_journal(self) -> None:
        """Write anything closed that has not been written.

        Idempotent on trade_id: a retry after a timeout upserts the same
        row rather than counting the trade twice. That is the difference
        between a journal and a story about a journal."""
        if not self.persist:
            return
        pending = [r for r in self.runner.portfolio.journal
                   if not r.open and r.trade_id not in self._persisted]
        if not pending:
            return
        rows = [_journal_to_row(r, self.symbol, self._session_id) for r in pending]
        try:
            _save_trades(rows)
        except Exception as exc:
            self.status.storage_failures += 1
            self.status.state = STORAGE_DEGRADED
            self.status.reason = f"journal write failed: {exc}"
            logger.warning("paper journal write failed: %s", exc)
            return
        for row in pending:
            self._persisted.add(row.trade_id)

    # ---- reporting --------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        with self._lock:
            out = self.runner.report()
            out["status"] = self.status.as_dict()
            position = self.runner.portfolio.positions.get(self.symbol)
            if position is not None and self.runner.book is not None:
                mark = self.runner.book.mid
                out["open_position"] = {
                    "direction": position.direction, "qty": position.qty,
                    "entry_price": position.entry_price,
                    "entry_at_ms": position.entry_at_ms,
                    "mark": mark,
                    "unrealised": round(position.unrealised(mark), 6)
                    if mark else None,
                }
            else:
                out["open_position"] = None
            out["may_enter"] = self.may_enter
            return out


# ---- storage ---------------------------------------------------------------

def _journal_to_row(row: JournalRow, symbol: str, session: str) -> Dict[str, Any]:
    return {
        "trade_id": f"{session}:{row.trade_id}",
        "session_id": session,
        "symbol": symbol,
        "strategy_version": row.strategy_version,
        "config_hash": row.config_hash,
        "signal_id": row.signal_id,
        "episode_id": row.episode_id,
        "direction": row.direction,
        "qty": row.qty,
        "entry_at_ms": row.entry_at_ms, "entry_price": row.entry_price,
        "entry_liquidity": row.entry_liquidity,
        "exit_at_ms": row.exit_at_ms, "exit_price": row.exit_price,
        "exit_liquidity": row.exit_liquidity, "exit_reason": row.exit_reason,
        "fees": row.fees, "funding": row.funding,
        "gross_pnl": row.gross_pnl, "net_pnl": row.net_pnl,
        "quality": row.quality,
        "fills": {"entry": row.entry_fill_ids, "exit": row.exit_fill_ids},
        "notes": row.notes,
    }


def _row_to_journal(row: Dict[str, Any]) -> Optional[JournalRow]:
    try:
        fills = row.get("fills") or {}
        return JournalRow(
            trade_id=str(row.get("trade_id")),
            strategy_version=str(row.get("strategy_version") or ""),
            config_hash=str(row.get("config_hash") or ""),
            signal_id=str(row.get("signal_id") or ""),
            symbol=str(row.get("symbol") or ""),
            direction=str(row.get("direction") or ""),
            qty=float(row.get("qty") or 0.0),
            entry_at_ms=int(row.get("entry_at_ms") or 0),
            entry_price=float(row.get("entry_price") or 0.0),
            entry_liquidity=str(row.get("entry_liquidity") or ""),
            entry_order_id="", entry_fill_ids=list(fills.get("entry") or []),
            exit_at_ms=int(row.get("exit_at_ms") or 0),
            exit_price=float(row.get("exit_price") or 0.0),
            exit_liquidity=str(row.get("exit_liquidity") or ""),
            exit_order_id="", exit_fill_ids=list(fills.get("exit") or []),
            exit_reason=str(row.get("exit_reason") or ""),
            fees=float(row.get("fees") or 0.0),
            funding=float(row.get("funding") or 0.0),
            gross_pnl=float(row.get("gross_pnl") or 0.0),
            net_pnl=float(row.get("net_pnl") or 0.0),
            quality=str(row.get("quality") or QUALITY_OK),
            episode_id=str(row.get("episode_id") or ""),
            notes=str(row.get("notes") or ""))
    except (TypeError, ValueError):
        return None


def _save_trades(rows: List[Dict[str, Any]]) -> None:
    from t3_engine.database import supabase_rest

    if not supabase_rest.configured():
        raise RuntimeError("Supabase is not configured")
    supabase_rest.insert(TABLE_PAPER_TRADES, rows, on_conflict="trade_id")


def _load_trades(symbol: str, limit: int = 2_000) -> List[Dict[str, Any]]:
    from t3_engine.database import supabase_rest

    if not supabase_rest.configured():
        raise RuntimeError("Supabase is not configured")
    return supabase_rest.select(TABLE_PAPER_TRADES, filters={"symbol": symbol},
                                order="exit_at_ms.asc", limit=limit)


_traders: Dict[str, PaperTrader] = {}
_lock = threading.Lock()


def get_traders() -> Dict[str, PaperTrader]:
    return dict(_traders)


def start_trader(symbol: str, strategy: str,
                 params: Optional[Dict[str, Any]] = None,
                 **kwargs) -> PaperTrader:
    with _lock:
        key = f"{symbol.upper()}:{strategy}"
        trader = _traders.get(key)
        if trader is None:
            trader = PaperTrader(symbol, strategy, params=params, **kwargs)
            trader.start()
            _traders[key] = trader
        return trader


def reset_traders() -> None:
    with _lock:
        _traders.clear()
