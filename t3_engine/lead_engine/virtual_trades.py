"""Virtual trades on the engine's OWN signals: the falsifiability layer.

A microstructure engine that only ever reports scores cannot be wrong
about anything. This module makes it wrong in public: every actionable
signal opens a virtual position, every position closes for a stated
reason, and the ledger says what it cost.

It is not a backtest and it is not a simulation. It runs on the live
stream, in real time, against the same book the signal was computed from,
and every rule below exists to stop it flattering itself.

THE RULES, and why each one is there:

1. ENTRY IS NEVER AT THE SIGNAL'S OWN PRICE. A signal fires at time T
   from a book observed at T. Filling at that book would be filling on
   information that arrived at the same instant as the decision, which is
   the oldest way to make a strategy look good. The intent waits for the
   NEXT book update strictly after T.

2. ENTRY CROSSES THE SPREAD. A long pays the ask, a short hits the bid,
   and configured slippage is added on top. Filling at the mid is a
   half-spread of free money per trade, which on a 1bp spread and a
   hundred trades is a strategy's entire edge.

3. NO FILL INVENTED INSIDE A GAP. If the stream breaks, the next book to
   arrive may be seconds and a long way from where the signal fired.
   Filling there is not pessimism, it is fiction: nobody knows what
   happened in between. An intent whose first fresh book arrives after
   `max_fill_gap_ms` is ABANDONED and recorded as such - a trade that did
   not happen, not a trade at a made-up price.

   The same applies on the way out. A position whose stop sits inside an
   observed gap is closed and flagged `gap_uncertain`, because the exit
   price is a guess and the ledger says so rather than quietly booking
   the stop.

4. EXITS ARE STOP, TARGET, OR TIME. A position that is never closed is a
   position that never loses. Every one carries a deadline.

5. FEES BOTH WAYS, ALWAYS. Taker in, taker out, deducted from the gross.
   A ledger that nets to zero after costs is a ledger that says so.

6. STALE BOOKS DO NOT FILL ANYTHING. If the book that would fill an order
   is older than the engine's own DEGRADED threshold, it is not a price,
   and no entry or exit is taken from it.

ONE CLOCK FOR ORDERING, THE OTHER FOR THE RECORD. A signal is stamped
with this process's clock; a book carries the exchange's. Comparing the
two directly would make the entry rule depend on the clock offset between
Frankfurt and Singapore - at 80ms of it, every fill is 80ms later than it
should be, and at a negative offset the ledger would happily fill on a
book stamped BEFORE the signal. So every ordering decision here - is this
book after the signal, is the gap too long, has the deadline passed - is
made on the ARRIVAL clock, which is the same clock the signal is stamped
with. The exchange stamp is kept alongside it and reported, because that
is when the price was true.

PERSISTENCE, AND WHY IT IS A HAND-OFF RATHER THAN A WRITE. A ledger that
lives only in memory answers nothing on a host that restarts: the free
Render plan stops the process after fifteen minutes without an inbound
request, which reset this P&L to zero roughly four times an hour. So a
trade that reaches a terminal state is queued on `pending_persist`, and
the recorder thread drains it.

Queued, NOT written. `_retire` is reached from the ingest thread, and an
HTTP call there would stall the socket reader - which is the exact
failure the two-thread ingest exists to prevent. This module therefore
performs no I/O at all, holds no database handle, and does not import
storage; it offers rows and something else takes them.

Nothing here can place a real order. It holds no credential, imports no
execution module, and writes only its own journal.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# States a signal must be in for a virtual position to open. WATCH and
# IDLE are not calls and do not trade.
ACTIONABLE = ("PRE_BREAK_LONG", "PRE_BREAK_SHORT", "HIGH_PROBABILITY",
              "A_PLUS", "REVERSAL_CANDIDATE")

PENDING = "PENDING"
OPEN = "OPEN"
CLOSED = "CLOSED"
ABANDONED = "ABANDONED"

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class LedgerConfig:
    """Costs and limits. Every one is a judgement call, so every one is
    a setting rather than a number buried in the arithmetic."""

    # Bybit's taker fee for linear perpetuals, as a fraction. Charged on
    # both sides because a virtual position that enters and exits pays
    # twice, and pretending otherwise is the commonest way a paper P&L
    # beats a real one.
    taker_fee: float = 0.00055
    # Added to the crossed price, in basis points. Covers the difference
    # between the top of book and what size actually gets.
    slippage_bps: float = 1.0
    # Notional per virtual trade, in quote currency.
    notional: float = 1_000.0
    # Stop and target, as a fraction of the entry price.
    stop_pct: float = 0.004
    target_pct: float = 0.006
    # A position still open after this is closed at the market. A trade
    # with no deadline is a trade that cannot lose.
    max_hold_ms: int = 15 * 60_000
    # An intent whose first fresh book arrives later than this is
    # abandoned rather than filled somewhere unknowable.
    max_fill_gap_ms: int = 3_000
    # A book older than this is not a price.
    max_book_age_ms: float = 1_000.0
    # Only one virtual position per symbol at a time: overlapping
    # positions on one signal source measure the signal's frequency, not
    # its quality.
    max_open: int = 1
    # How many closed trades the journal keeps in memory.
    journal_limit: int = 500


@dataclass
class VirtualTrade:
    id: str
    symbol: str
    direction: str
    status: str = PENDING

    # The signal that asked for it.
    signal_state: str = ""
    signal_at_ms: int = 0
    signal_price: Optional[float] = None
    break_score: float = 0.0
    confidence: float = 0.0

    # The fill. `entry_at_ms` is on the arrival clock (comparable with
    # signal_at_ms); `entry_book_ms` is the exchange's own stamp for the
    # book that filled it.
    entry_at_ms: Optional[int] = None
    entry_book_ms: Optional[int] = None
    entry_price: Optional[float] = None
    entry_reference: Optional[float] = None      # touch price before slippage
    entry_slippage: float = 0.0
    quantity: float = 0.0

    stop: Optional[float] = None
    target: Optional[float] = None
    deadline_ms: Optional[int] = None

    # The exit.
    exit_at_ms: Optional[int] = None
    exit_book_ms: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reference: Optional[float] = None       # touch price before slippage
    exit_reason: str = ""
    exit_slippage: float = 0.0

    # The timestamp of the last book this position was actually marked
    # against. It is what makes RULE 3 checkable on the way out: if the
    # next book arrives long after this one, the stop may have been
    # crossed in the dark, and the exit is flagged rather than trusted.
    last_seen_ms: Optional[int] = None

    fee_entry: float = 0.0
    fee_exit: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0

    # Set when an exit had to be taken across an observed gap, so the
    # price is a guess. The trade still counts; the flag says how much to
    # trust it.
    gap_uncertain: bool = False
    note: str = ""

    @property
    def fees(self) -> float:
        return self.fee_entry + self.fee_exit

    def to_row(self) -> Dict[str, Any]:
        """The storage shape. `trade_id` is the natural key - the same
        trade offered twice must not become two rows, and the id already
        carries symbol, signal time and sequence."""
        return {
            "trade_id": self.id,
            "symbol": self.symbol,
            "direction": self.direction,
            "status": self.status,
            "signal_state": self.signal_state,
            "signal_at_ms": self.signal_at_ms,
            "signal_price": self.signal_price,
            "break_score": self.break_score,
            "confidence": self.confidence,
            "entry_at_ms": self.entry_at_ms,
            "entry_book_ms": self.entry_book_ms,
            "entry_price": self.entry_price,
            "entry_reference": self.entry_reference,
            "entry_slippage": self.entry_slippage,
            "quantity": self.quantity,
            "stop": self.stop,
            "target": self.target,
            "exit_at_ms": self.exit_at_ms,
            "exit_book_ms": self.exit_book_ms,
            "exit_price": self.exit_price,
            "exit_reference": self.exit_reference,
            "exit_reason": self.exit_reason,
            "exit_slippage": self.exit_slippage,
            "fee_entry": self.fee_entry,
            "fee_exit": self.fee_exit,
            "gross_pnl": self.gross_pnl,
            "net_pnl": self.net_pnl,
            "gap_uncertain": self.gap_uncertain,
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "VirtualTrade":
        """Back out of storage. Unknown columns are ignored rather than
        raising: a row written by a newer build must not stop an older
        one from reading its own history."""
        def number(key: str) -> Optional[float]:
            value = row.get(key)
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None

        def whole(key: str) -> Optional[int]:
            value = number(key)
            return None if value is None else int(value)

        return cls(
            id=str(row.get("trade_id") or ""),
            symbol=str(row.get("symbol") or ""),
            direction=str(row.get("direction") or ""),
            status=str(row.get("status") or CLOSED),
            signal_state=str(row.get("signal_state") or ""),
            signal_at_ms=whole("signal_at_ms") or 0,
            signal_price=number("signal_price"),
            break_score=number("break_score") or 0.0,
            confidence=number("confidence") or 0.0,
            entry_at_ms=whole("entry_at_ms"),
            entry_book_ms=whole("entry_book_ms"),
            entry_price=number("entry_price"),
            entry_reference=number("entry_reference"),
            entry_slippage=number("entry_slippage") or 0.0,
            quantity=number("quantity") or 0.0,
            stop=number("stop"), target=number("target"),
            exit_at_ms=whole("exit_at_ms"),
            exit_book_ms=whole("exit_book_ms"),
            exit_price=number("exit_price"),
            exit_reference=number("exit_reference"),
            exit_reason=str(row.get("exit_reason") or ""),
            exit_slippage=number("exit_slippage") or 0.0,
            fee_entry=number("fee_entry") or 0.0,
            fee_exit=number("fee_exit") or 0.0,
            gross_pnl=number("gross_pnl") or 0.0,
            net_pnl=number("net_pnl") or 0.0,
            gap_uncertain=bool(row.get("gap_uncertain")),
            note=str(row.get("note") or ""),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "symbol": self.symbol, "direction": self.direction,
            "status": self.status,
            "signal_state": self.signal_state, "signal_at_ms": self.signal_at_ms,
            "signal_price": self.signal_price,
            "break_score": round(self.break_score, 2),
            "confidence": round(self.confidence, 3),
            "entry_at_ms": self.entry_at_ms,
            "entry_book_ms": self.entry_book_ms,
            "entry_price": self.entry_price,
            "entry_reference": self.entry_reference,
            "entry_slippage": round(self.entry_slippage, 8),
            "quantity": round(self.quantity, 8),
            "stop": self.stop, "target": self.target,
            "deadline_ms": self.deadline_ms,
            "exit_at_ms": self.exit_at_ms, "exit_book_ms": self.exit_book_ms,
            "exit_price": self.exit_price,
            "exit_reference": self.exit_reference,
            "exit_reason": self.exit_reason,
            "exit_slippage": round(self.exit_slippage, 8),
            "fee_entry": round(self.fee_entry, 8),
            "fee_exit": round(self.fee_exit, 8),
            "fees": round(self.fees, 8),
            "gross_pnl": round(self.gross_pnl, 8),
            "net_pnl": round(self.net_pnl, 8),
            "gap_uncertain": self.gap_uncertain,
            "note": self.note,
            # How long the signal took to become a fill, which is the
            # number that says whether the entry rule is costing anything.
            "fill_delay_ms": (None if self.entry_at_ms is None or not self.signal_at_ms
                              else self.entry_at_ms - self.signal_at_ms),
            "hold_ms": (None if self.exit_at_ms is None or self.entry_at_ms is None
                        else self.exit_at_ms - self.entry_at_ms),
        }


class VirtualLedger:
    """One symbol's virtual book. Driven by the live stream, never by a
    clock of its own."""

    def __init__(self, symbol: str, config: Optional[LedgerConfig] = None) -> None:
        self.symbol = symbol.upper()
        self.config = config or LedgerConfig()
        # Signals arrive on whichever thread computes the snapshot; books
        # arrive on the ingest thread. Both mutate the same three lists,
        # and a fill that races a settle would leave a position in both
        # of them.
        self._lock = threading.RLock()
        self.pending: List[VirtualTrade] = []
        self.open: List[VirtualTrade] = []
        self.closed: List[VirtualTrade] = []
        # Terminal trades waiting to be filed. Handed off, never written
        # here - see the module docstring: `_retire` runs on the ingest
        # thread, and an HTTP call there stalls the socket reader.
        self.pending_persist: List[VirtualTrade] = []
        # How many of `closed` came back from storage rather than from
        # this process. Reported, because "42 trades" means something
        # different when 40 of them predate the current run.
        self.restored = 0
        self._last_signal_at = 0
        self._sequence = 0
        # Counters that make the rules auditable rather than claimed.
        self.abandoned_gap = 0
        self.skipped_stale_book = 0
        self.skipped_already_open = 0
        self.gap_uncertain_exits = 0

    # ---- signals ----

    def on_signal(self, signal: Dict[str, Any], now_ms: Optional[int] = None) -> Optional[VirtualTrade]:
        """A signal transition may open ONE intent.

        Keyed on `changed_at`, which does not move while a state persists,
        so a state held for forty seconds opens one intent rather than
        eighty."""
        with self._lock:
            return self._on_signal(signal, now_ms)

    def _on_signal(self, signal: Dict[str, Any],
                   now_ms: Optional[int]) -> Optional[VirtualTrade]:
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        state = str(signal.get("state") or "")
        if state not in ACTIONABLE:
            return None
        direction = str(signal.get("direction") or "")
        if direction not in (LONG, SHORT):
            return None

        changed_at = signal.get("changed_at")
        signal_at = int(float(changed_at) * 1000) if changed_at else now_ms
        if signal_at <= self._last_signal_at:
            return None                      # the same transition, seen again
        self._last_signal_at = signal_at

        if len(self.open) + len(self.pending) >= self.config.max_open:
            self.skipped_already_open += 1
            return None

        self._sequence += 1
        trade = VirtualTrade(
            id=f"{self.symbol}-{signal_at}-{self._sequence}",
            symbol=self.symbol, direction=direction,
            signal_state=state, signal_at_ms=signal_at,
            signal_price=_number(signal.get("price")),
            break_score=float(signal.get("break_probability") or 0.0),
            confidence=float(signal.get("confidence") or 0.0),
        )
        self.pending.append(trade)
        return trade

    # ---- the book ----

    def on_book(self, best_bid: Optional[float], best_ask: Optional[float],
                book_at_ms: int, received_at_ms: Optional[int] = None,
                book_age_ms: Optional[float] = None) -> None:
        """One book update. Fills what can be filled, closes what must be.

        This is the ONLY place a price enters the ledger.

        `book_at_ms` is the exchange's stamp and is only recorded;
        `received_at_ms` is when this process saw it and is what every
        ordering decision is made on - see the module docstring."""
        with self._lock:
            self._on_book(best_bid, best_ask, book_at_ms, received_at_ms,
                          book_age_ms)

    def _on_book(self, best_bid: Optional[float], best_ask: Optional[float],
                 book_at_ms: int, received_at_ms: Optional[int],
                 book_age_ms: Optional[float]) -> None:
        at_ms = int(received_at_ms if received_at_ms else (book_at_ms or time.time() * 1000))
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
            return
        if book_age_ms is not None and book_age_ms > self.config.max_book_age_ms:
            # Not a price. Counted, because a ledger that silently does
            # nothing looks identical to one that is working.
            if self.pending:
                self.skipped_stale_book += 1
            return
        self._fill_pending(best_bid, best_ask, at_ms, int(book_at_ms or 0))
        self._settle_open(best_bid, best_ask, at_ms, int(book_at_ms or 0))

    def _fill_pending(self, bid: float, ask: float, at_ms: int,
                      book_ms: int) -> None:
        still_pending: List[VirtualTrade] = []
        for trade in self.pending:
            # RULE 1: strictly after the signal. A book that arrived at or
            # before the signal is one the signal was computed from.
            if at_ms <= trade.signal_at_ms:
                still_pending.append(trade)
                continue
            # RULE 3: no fill invented inside a gap.
            if at_ms - trade.signal_at_ms > self.config.max_fill_gap_ms:
                trade.status = ABANDONED
                trade.note = (f"no fresh book within "
                              f"{self.config.max_fill_gap_ms}ms of the signal - "
                              f"first was {at_ms - trade.signal_at_ms}ms later; "
                              f"not filled at a price nobody saw")
                self.abandoned_gap += 1
                self._retire(trade)
                continue
            self._fill(trade, bid, ask, at_ms, book_ms)
        self.pending = still_pending

    def _fill(self, trade: VirtualTrade, bid: float, ask: float, at_ms: int,
              book_ms: int = 0) -> None:
        # RULE 2: cross the spread, then pay slippage on top.
        reference = ask if trade.direction == LONG else bid
        slip = reference * self.config.slippage_bps / 10_000.0
        price = reference + slip if trade.direction == LONG else reference - slip

        trade.entry_reference = reference
        trade.entry_slippage = abs(price - reference)
        trade.entry_price = price
        trade.entry_at_ms = at_ms
        trade.entry_book_ms = book_ms or None
        trade.quantity = self.config.notional / price
        trade.fee_entry = self.config.notional * self.config.taker_fee
        if trade.direction == LONG:
            trade.stop = price * (1.0 - self.config.stop_pct)
            trade.target = price * (1.0 + self.config.target_pct)
        else:
            trade.stop = price * (1.0 + self.config.stop_pct)
            trade.target = price * (1.0 - self.config.target_pct)
        trade.deadline_ms = at_ms + self.config.max_hold_ms
        trade.status = OPEN
        self.open.append(trade)

    def _settle_open(self, bid: float, ask: float, at_ms: int,
                     book_ms: int = 0) -> None:
        still_open: List[VirtualTrade] = []
        for trade in self.open:
            # RULE 1 applies to the exit as well: the book that FILLED a
            # position does not also settle it. Otherwise a spread wider
            # than the stop closes every trade on entry - the long pays
            # the ask, the bid is a full spread below it, and the ledger
            # books a stop that no price movement caused. The exit is
            # read from the next book, like the entry.
            if trade.entry_at_ms == at_ms:
                still_open.append(trade)
                continue
            # Exiting crosses the spread the other way.
            touch = bid if trade.direction == LONG else ask
            reason = ""
            if trade.direction == LONG:
                if touch <= (trade.stop or 0.0):
                    reason = "stop"
                elif touch >= (trade.target or float("inf")):
                    reason = "target"
            else:
                if touch >= (trade.stop or float("inf")):
                    reason = "stop"
                elif touch <= (trade.target or 0.0):
                    reason = "target"
            if not reason and trade.deadline_ms is not None and \
                    at_ms >= trade.deadline_ms:
                reason = "time"
            if not reason:
                still_open.append(trade)
                continue

            # RULE 3, on the way out: if the level was crossed inside an
            # observed gap, the exit price is a guess. Take it, and say so.
            previous = trade.last_seen_ms
            if previous is None:
                previous = trade.entry_at_ms
            gap = at_ms - previous if previous is not None else 0
            self._close(trade, touch, at_ms, reason, book_ms=book_ms,
                        uncertain=gap > self.config.max_fill_gap_ms)
        self.open = still_open
        for trade in still_open:
            trade.last_seen_ms = at_ms

    def _close(self, trade: VirtualTrade, touch: float, at_ms: int,
               reason: str, book_ms: int = 0, uncertain: bool = False) -> None:
        slip = touch * self.config.slippage_bps / 10_000.0
        price = touch - slip if trade.direction == LONG else touch + slip
        trade.exit_reference = touch
        trade.exit_slippage = abs(price - touch)
        trade.exit_price = price
        trade.exit_at_ms = at_ms
        trade.exit_book_ms = book_ms or None
        trade.exit_reason = reason
        trade.fee_exit = price * trade.quantity * self.config.taker_fee
        entry = trade.entry_price or price
        if trade.direction == LONG:
            trade.gross_pnl = (price - entry) * trade.quantity
        else:
            trade.gross_pnl = (entry - price) * trade.quantity
        trade.net_pnl = trade.gross_pnl - trade.fees
        trade.status = CLOSED
        if uncertain:
            trade.gap_uncertain = True
            trade.note = ("the level was crossed inside a gap in the stream - "
                          "this exit price is an estimate, not an observation")
            self.gap_uncertain_exits += 1
        self._retire(trade)

    def _retire(self, trade: VirtualTrade) -> None:
        self.closed.append(trade)
        if len(self.closed) > self.config.journal_limit:
            self.closed = self.closed[-self.config.journal_limit:]
        self.pending_persist.append(trade)

    # ---- persistence: offered, not performed ----

    def drain_persist(self) -> List[Dict[str, Any]]:
        """Terminal trades since the last drain, as storage rows.

        Called by whatever owns the database connection, on its own
        thread. The ledger does not know where these go."""
        with self._lock:
            rows = [trade.to_row() for trade in self.pending_persist]
            self.pending_persist = []
        return rows

    def restore(self, rows: List[Dict[str, Any]]) -> int:
        """Seed the journal from storage. Returns how many were taken.

        Only terminal trades are restored, and only into `closed`: an
        OPEN position from a dead process is not open - nobody has been
        marking it against the book, its stop was never checked, and
        resurrecting it would book an exit at a price that was never
        observed. That is the same rule as RULE 3, applied to a gap the
        size of a restart.

        Existing ids are skipped, so restoring twice is not a doubled
        P&L, and nothing restored is queued for writing back."""
        with self._lock:
            known = {trade.id for trade in
                     self.closed + self.open + self.pending}
            taken = 0
            for row in rows:
                try:
                    trade = VirtualTrade.from_row(row)
                except Exception:               # noqa: BLE001 - one bad row
                    continue                    # must not lose the rest
                if not trade.id or trade.id in known:
                    continue
                if trade.status not in (CLOSED, ABANDONED):
                    continue
                known.add(trade.id)
                self.closed.append(trade)
                taken += 1
            # Oldest first, then trimmed to the journal's own bound, so a
            # long history restores its most recent tail.
            self.closed.sort(key=lambda t: t.signal_at_ms)
            if len(self.closed) > self.config.journal_limit:
                self.closed = self.closed[-self.config.journal_limit:]
            self.restored += taken
            # A restored trade must not be written back: it came FROM
            # storage, and re-queueing it would grow the table by its own
            # contents on every restart.
            return taken

    # ---- reporting ----

    def open_pnl(self, bid: Optional[float], ask: Optional[float]) -> float:
        """Mark to market at what it would cost to CLOSE, not at the mid.
        An open position valued at the mid is a position carrying an
        unbooked half-spread of profit."""
        if bid is None or ask is None:
            return 0.0
        total = 0.0
        for trade in self.open:
            touch = bid if trade.direction == LONG else ask
            entry = trade.entry_price or touch
            gross = ((touch - entry) if trade.direction == LONG
                     else (entry - touch)) * trade.quantity
            # The exit fee is not yet paid but is certain to be.
            total += gross - trade.fee_entry - touch * trade.quantity * self.config.taker_fee
        return total

    def summary(self, bid: Optional[float] = None,
                ask: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            return self._summary(bid, ask)

    def _summary(self, bid: Optional[float], ask: Optional[float]) -> Dict[str, Any]:
        settled = [t for t in self.closed if t.status == CLOSED]
        wins = [t for t in settled if t.net_pnl > 0]
        losses = [t for t in settled if t.net_pnl < 0]
        gross = sum(t.gross_pnl for t in settled)
        fees = sum(t.fees for t in settled)
        net = sum(t.net_pnl for t in settled)
        by_reason: Dict[str, int] = {}
        for trade in settled:
            by_reason[trade.exit_reason] = by_reason.get(trade.exit_reason, 0) + 1
        return {
            "symbol": self.symbol,
            "open_positions": len(self.open),
            "pending_intents": len(self.pending),
            "closed_trades": len(settled),
            "abandoned": len([t for t in self.closed if t.status == ABANDONED]),
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": (round(100.0 * len(wins) / len(settled), 2)
                             if settled else None),
            "gross_pnl": round(gross, 6),
            "fees_paid": round(fees, 6),
            "net_pnl": round(net, 6),
            # The number that matters most on a short sample: costs can
            # exceed a real edge, and this says by how much.
            "fees_vs_gross_pct": (round(100.0 * fees / abs(gross), 2)
                                  if gross else None),
            "open_pnl": round(self.open_pnl(bid, ask), 6),
            "exits_by_reason": by_reason,
            "gap_uncertain_exits": self.gap_uncertain_exits,
            # How much of the sample predates this process. Without it,
            # "42 trades" reads as 42 trades this run.
            "restored_from_storage": self.restored,
            "abandoned_on_gap": self.abandoned_gap,
            "skipped_stale_book": self.skipped_stale_book,
            "skipped_already_open": self.skipped_already_open,
            "config": {
                "notional": self.config.notional,
                "taker_fee": self.config.taker_fee,
                "slippage_bps": self.config.slippage_bps,
                "stop_pct": self.config.stop_pct,
                "target_pct": self.config.target_pct,
                "max_hold_ms": self.config.max_hold_ms,
                "max_fill_gap_ms": self.config.max_fill_gap_ms,
            },
        }

    def journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return self._journal(limit)

    def _journal(self, limit: int) -> List[Dict[str, Any]]:
        rows = [t.as_dict() for t in self.closed[-limit:]]
        rows.extend(t.as_dict() for t in self.open)
        rows.extend(t.as_dict() for t in self.pending)
        return sorted(rows, key=lambda r: r.get("signal_at_ms") or 0, reverse=True)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
