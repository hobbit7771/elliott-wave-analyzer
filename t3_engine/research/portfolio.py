"""Positions, the journal, and a P&L that cannot flatter itself.

FOUR WAYS A BACKTEST LIES ABOUT MONEY, and what stops each here.

  1. Reporting only what closed. A run that ends with the winners
     realised and the losers still open shows a profit it does not have.
     `mark_to_market` values every open position by SELLING IT INTO THE
     BOOK - the executable side, walking real depth, paying the taker fee
     - and `finalise` refuses to produce a result without doing it.

  2. Charging the spread twice. A fill price out of the simulator already
     contains the spread and the depth walked. Adding a slippage term on
     top charges it again. Only FEES and FUNDING are subtracted here, and
     `gross_pnl` is computed from fill prices alone.

  3. Netting a position that was never flat. Average-cost accounting
     across a reversal hides the round trip inside it. A position that
     crosses zero is CLOSED and a new one opened, so every round trip is
     its own row with its own entry, exit and reason.

  4. Losing the reason. A journal row without the exit reason and the
     data-quality flag cannot be audited later, so both are required
     fields rather than optional decorations.

RISK LIMITS ARE PRE-TRADE, NOT A POST-HOC FILTER. `RiskLimits.allows()`
is asked BEFORE an order is sent, and a refusal is recorded as a
NO_TRADE reason rather than silently skipped - "the strategy declined"
and "the strategy never looked" are different facts.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from t3_engine.research.book import BUY, SELL, BookState
from t3_engine.research.costs import MAKER, TAKER, CostModel, FeeSchedule
from t3_engine.research.execution import Fill

LONG = "long"
SHORT = "short"

# Exit reasons. A closed trade must carry one of these.
EXIT_TARGET = "TARGET"
EXIT_STOP = "STOP"
EXIT_TIME = "TIME_STOP"
EXIT_SIGNAL = "SIGNAL_EXIT"
EXIT_INVALIDATED = "INVALIDATED"
EXIT_RISK = "RISK_LIMIT"
EXIT_FINALISED = "MARKED_TO_BOOK"      # open at the end of the run
EXIT_DATA_GAP = "DATA_GAP_EXIT"

# Data-quality flags that travel with a trade.
QUALITY_OK = "OK"
QUALITY_DATA_GAP = "DATA_GAP"
QUALITY_STALE = "STALE_BOOK"
QUALITY_DEGRADED = "DEGRADED"


@dataclass
class RiskLimits:
    """Fixed BEFORE the research, so a limit cannot be relaxed to rescue
    a result. Comparing two strategies at different risk is comparing
    nothing."""
    capital: float = 10_000.0
    max_position_notional: float = 2_000.0
    max_symbol_notional: float = 1_000.0
    max_total_notional: float = 3_000.0
    max_concurrent_positions: int = 3
    max_daily_loss: float = 200.0
    max_drawdown: float = 500.0
    # Correlated instruments share a budget: three alt-coin longs is one
    # bet on the same thing wearing three hats.
    max_correlated_notional: float = 1_500.0
    correlation_group: Dict[str, str] = field(default_factory=dict)

    def group_of(self, symbol: str) -> str:
        return self.correlation_group.get(symbol.upper(), "DEFAULT")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capital": self.capital,
            "max_position_notional": self.max_position_notional,
            "max_symbol_notional": self.max_symbol_notional,
            "max_total_notional": self.max_total_notional,
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_daily_loss": self.max_daily_loss,
            "max_drawdown": self.max_drawdown,
            "max_correlated_notional": self.max_correlated_notional,
        }


@dataclass
class JournalRow:
    """One round trip, from flat to flat."""
    trade_id: str
    strategy_version: str
    config_hash: str
    signal_id: str
    symbol: str
    direction: str
    qty: float
    entry_at_ms: int
    entry_price: float
    entry_liquidity: str
    entry_order_id: str
    entry_fill_ids: List[str]
    exit_at_ms: int = 0
    exit_price: float = 0.0
    exit_liquidity: str = ""
    exit_order_id: str = ""
    exit_fill_ids: List[str] = field(default_factory=list)
    exit_reason: str = ""
    fees: float = 0.0
    funding: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    quality: str = QUALITY_OK
    episode_id: str = ""
    notes: str = ""

    @property
    def open(self) -> bool:
        return not self.exit_reason

    @property
    def hold_ms(self) -> int:
        return max(0, self.exit_at_ms - self.entry_at_ms) if self.exit_at_ms else 0

    @property
    def net_bps(self) -> float:
        notional = self.entry_price * self.qty
        return 10_000.0 * self.net_pnl / notional if notional else 0.0

    @property
    def gross_bps(self) -> float:
        notional = self.entry_price * self.qty
        return 10_000.0 * self.gross_pnl / notional if notional else 0.0

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items()}
        out["hold_ms"] = self.hold_ms
        out["net_bps"] = round(self.net_bps, 4)
        out["gross_bps"] = round(self.gross_bps, 4)
        return out


@dataclass
class Position:
    symbol: str
    direction: str
    qty: float
    entry_price: float
    entry_at_ms: int
    entry_liquidity: str
    entry_order_id: str
    entry_fill_ids: List[str] = field(default_factory=list)
    signal_id: str = ""
    episode_id: str = ""
    fees: float = 0.0
    funding: float = 0.0
    quality: str = QUALITY_OK

    @property
    def notional(self) -> float:
        return self.entry_price * self.qty

    def unrealised(self, mark: float) -> float:
        if self.direction == LONG:
            return (mark - self.entry_price) * self.qty
        return (self.entry_price - mark) * self.qty


def config_hash(config: Dict[str, Any]) -> str:
    """A stable fingerprint of the parameters a result was produced with.
    Without it a journal row cannot be tied to the settings that made it,
    and 'we ran it again and got something else' has no explanation."""
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Portfolio:
    def __init__(self, *, strategy_version: str, config: Dict[str, Any],
                 limits: Optional[RiskLimits] = None,
                 fees: Optional[FeeSchedule] = None) -> None:
        self.strategy_version = strategy_version
        self.config = dict(config)
        self.config_hash = config_hash(config)
        self.limits = limits or RiskLimits()
        self.costs = CostModel(fees)
        self.positions: Dict[str, Position] = {}
        self.journal: List[JournalRow] = []
        self.no_trade_reasons: Dict[str, int] = {}
        self._ids = itertools.count(1)
        self.equity_curve: List[Tuple[int, float]] = []
        self.finalised = False
        self._peak_equity = 0.0
        self._max_drawdown = 0.0
        self._day_pnl: Dict[int, float] = {}

    # ---- pre-trade ------------------------------------------------------

    def decline(self, reason: str) -> None:
        """Record that the strategy LOOKED and said no. A NO_TRADE run
        with reasons is a result; one without is an absence of data."""
        self.no_trade_reasons[reason] = self.no_trade_reasons.get(reason, 0) + 1

    def allows(self, symbol: str, notional: float, at_ms: int) -> Tuple[bool, str]:
        symbol = symbol.upper()
        if symbol in self.positions:
            return False, "already holding this symbol"
        if len(self.positions) >= self.limits.max_concurrent_positions:
            return False, "max_concurrent_positions"
        if notional > self.limits.max_position_notional:
            return False, "max_position_notional"
        if notional > self.limits.max_symbol_notional:
            return False, "max_symbol_notional"
        total = sum(p.notional for p in self.positions.values()) + notional
        if total > self.limits.max_total_notional:
            return False, "max_total_notional"

        group = self.limits.group_of(symbol)
        correlated = notional + sum(
            p.notional for s, p in self.positions.items()
            if self.limits.group_of(s) == group)
        if correlated > self.limits.max_correlated_notional:
            return False, "max_correlated_notional"

        day = at_ms // 86_400_000
        if self._day_pnl.get(day, 0.0) <= -abs(self.limits.max_daily_loss):
            return False, "max_daily_loss"
        if self._max_drawdown >= abs(self.limits.max_drawdown):
            return False, "max_drawdown"
        return True, ""

    # ---- lifecycle ------------------------------------------------------

    def open_position(self, *, symbol: str, direction: str, fills: Sequence[Fill],
                      signal_id: str, episode_id: str = "",
                      quality: str = QUALITY_OK) -> Optional[Position]:
        fills = [f for f in fills if f.qty > 0]
        if not fills:
            return None
        qty = sum(f.qty for f in fills)
        price = sum(f.price * f.qty for f in fills) / qty
        position = Position(
            symbol=symbol.upper(), direction=direction, qty=qty, entry_price=price,
            entry_at_ms=max(f.at_ms for f in fills),
            entry_liquidity=MAKER if all(f.liquidity == MAKER for f in fills) else TAKER,
            entry_order_id=fills[0].order_id,
            entry_fill_ids=[f.fill_id for f in fills],
            signal_id=signal_id, episode_id=episode_id,
            fees=sum(f.fee for f in fills), quality=quality)
        self.positions[position.symbol] = position
        return position

    def close_position(self, symbol: str, *, fills: Sequence[Fill], reason: str,
                       funding: float = 0.0,
                       quality: Optional[str] = None) -> Optional[JournalRow]:
        symbol = symbol.upper()
        position = self.positions.pop(symbol, None)
        if position is None:
            return None
        fills = [f for f in fills if f.qty > 0]
        if not fills:
            # Nothing closed it. Put it back rather than losing it: a
            # position that disappears from the book is a loss nobody
            # will ever see.
            self.positions[symbol] = position
            return None

        qty = sum(f.qty for f in fills)
        price = sum(f.price * f.qty for f in fills) / qty
        exit_fees = sum(f.fee for f in fills)

        gross = ((price - position.entry_price) if position.direction == LONG
                 else (position.entry_price - price)) * min(qty, position.qty)
        fees = position.fees + exit_fees
        total_funding = position.funding + funding
        row = JournalRow(
            trade_id=f"t{next(self._ids)}",
            strategy_version=self.strategy_version, config_hash=self.config_hash,
            signal_id=position.signal_id, symbol=symbol,
            direction=position.direction, qty=position.qty,
            entry_at_ms=position.entry_at_ms, entry_price=position.entry_price,
            entry_liquidity=position.entry_liquidity,
            entry_order_id=position.entry_order_id,
            entry_fill_ids=list(position.entry_fill_ids),
            exit_at_ms=max(f.at_ms for f in fills), exit_price=price,
            exit_liquidity=MAKER if all(f.liquidity == MAKER for f in fills) else TAKER,
            exit_order_id=fills[0].order_id,
            exit_fill_ids=[f.fill_id for f in fills],
            exit_reason=reason, fees=fees, funding=total_funding,
            gross_pnl=gross,
            # The fill prices already carry the spread and the depth
            # walked. Only the exchange's cut and funding are still owed.
            net_pnl=gross - fees - total_funding,
            quality=quality or position.quality, episode_id=position.episode_id)
        self.journal.append(row)
        self._note_equity(row)
        return row

    def _note_equity(self, row: JournalRow) -> None:
        realised = sum(r.net_pnl for r in self.journal)
        self.equity_curve.append((row.exit_at_ms, realised))
        self._peak_equity = max(self._peak_equity, realised)
        self._max_drawdown = max(self._max_drawdown, self._peak_equity - realised)
        day = row.exit_at_ms // 86_400_000
        self._day_pnl[day] = self._day_pnl.get(day, 0.0) + row.net_pnl

    # ---- the end of a run ------------------------------------------------

    def mark_to_market(self, books: Dict[str, BookState], at_ms: int,
                       reason: str = EXIT_FINALISED) -> List[JournalRow]:
        """Value every open position by SELLING IT INTO THE BOOK.

        Not at the mid - the mid is not a price anyone can trade at. A
        long is closed against the BIDS and a short against the ASKS,
        walking real depth and paying the taker fee, because that is what
        getting flat would actually have cost.
        """
        rows = []
        for symbol in list(self.positions):
            position = self.positions[symbol]
            book = books.get(symbol.upper())
            if book is None or not book.valid:
                # No book to value it against. It stays open and is
                # reported as such rather than being marked at a price
                # that does not exist.
                continue
            side = "bid" if position.direction == LONG else "ask"
            price, filled, _ = book.walk(side, position.qty)
            if filled <= 0:
                continue
            fee = price * filled * self.costs.fees.taker_bps / 10_000.0
            synthetic = Fill(fill_id=f"mark:{symbol}", order_id=f"mark:{symbol}",
                             symbol=symbol, side=SELL if position.direction == LONG else BUY,
                             price=price, qty=filled, liquidity=TAKER,
                             at_ms=at_ms, fee=fee)
            row = self.close_position(symbol, fills=[synthetic], reason=reason)
            if row is not None:
                row.notes = (f"open at the end of the run; valued by walking "
                             f"{filled:g} into the {side}s")
                rows.append(row)
        return rows

    def finalise(self, books: Dict[str, BookState], at_ms: int) -> None:
        self.mark_to_market(books, at_ms)
        self.finalised = True

    # ---- reporting -------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        closed = [r for r in self.journal if not r.open]
        wins = [r for r in closed if r.net_pnl > 0]
        losses = [r for r in closed if r.net_pnl < 0]
        gross_win = sum(r.net_pnl for r in wins)
        gross_loss = -sum(r.net_pnl for r in losses)
        net = sum(r.net_pnl for r in closed)
        turnover = sum(r.entry_price * r.qty * 2 for r in closed)
        bps = [r.net_bps for r in closed]
        return {
            "strategy_version": self.strategy_version,
            "config_hash": self.config_hash,
            "finalised": self.finalised,
            "still_open": len(self.positions),
            "trades": len(closed),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
            "gross_pnl": round(sum(r.gross_pnl for r in closed), 6),
            "fees": round(sum(r.fees for r in closed), 6),
            "funding": round(sum(r.funding for r in closed), 6),
            "net_pnl": round(net, 6),
            "expectancy_bps": round(sum(bps) / len(bps), 4) if bps else 0.0,
            "avg_win": round(gross_win / len(wins), 6) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 6) if losses else 0.0,
            # NOT infinity when there are no losses. "No losing trade yet"
            # is not "infinitely profitable", it is a sample too small to
            # have found one - and infinity is not JSON, so emitting it
            # also destroyed the experiment row it was meant to describe.
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0
            else None,
            "profit_factor_undefined_reason": (
                None if gross_loss > 0
                else ("no losing trades in the sample" if wins
                      else "no closed trades")),
            "max_drawdown": round(self._max_drawdown, 6),
            "turnover": round(turnover, 2),
            "avg_hold_seconds": round(
                sum(r.hold_ms for r in closed) / len(closed) / 1000.0, 1)
            if closed else 0.0,
            "maker_entries": sum(1 for r in closed if r.entry_liquidity == MAKER),
            "maker_exits": sum(1 for r in closed if r.exit_liquidity == MAKER),
            "data_gap_trades": sum(1 for r in closed if r.quality != QUALITY_OK),
            "exit_reasons": _counts(r.exit_reason for r in closed),
            "no_trade_reasons": dict(self.no_trade_reasons),
            "episodes": len({r.episode_id for r in closed if r.episode_id}),
        }


def _counts(values: Iterable[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out
