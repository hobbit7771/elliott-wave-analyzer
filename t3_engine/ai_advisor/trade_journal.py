"""The record of what the agent's counts actually did.

A live session runs for days. A take-profit fires hours after the count
that planned it, a stop fires overnight, and until this existed every one
of those events lived in one process's memory and died with it. Two
consequences, both visible on a real screen:

  - A position with two of its four take-profit legs filled showed as
    "open positions: 1, closed trades: 0" and nothing else. The two fills
    were real, the money was real, and the dashboard could not say so.
  - The agent was asked to label the same instrument again and again with
    no idea whether its previous label made or lost anything.

This module fixes both by writing every fill down as it happens, tagged
with the count that produced it.

Deliberate limit: it records, it does not instruct. `summary_for` returns
counts and sums - trades, wins, realized P&L - and the analyst's brief
states them as fact. Nothing anywhere tells the model "your last count
lost, so try something else", because that is precisely how a model is
talked into fitting its next answer to the last result instead of to the
chart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from t3_engine.ai_advisor import analysis_store
from t3_engine.database import supabase_rest
from t3_engine.database.models import AiTradeEventRow
from t3_engine.database.session import init_db, make_session_factory, session_scope

_factory = None

# A live instrument accumulates events indefinitely. Reads are always
# bounded by this - a brief that carried a thousand fills would cost more
# context than the chart it is about.
MAX_EVENTS_RETURNED = 200


def _sessions(database_url: Optional[str] = None):
    """Same database as the analysis cache, read through the same knob, so
    a test that redirects one redirects both and cannot end up writing a
    journal into the real file."""
    global _factory
    if _factory is None or database_url:
        engine = init_db(database_url or analysis_store.DEFAULT_DATABASE_URL)
        factory = make_session_factory(engine)
        if database_url:
            return factory          # an explicit URL is not cached globally
        _factory = factory
    return _factory


@dataclass
class TradeEvent:
    source: str
    symbol: str
    timeframe: str
    position_id: str
    event: str
    side: str
    price: float
    quantity: float
    realized_pnl: float = 0.0
    position_realized_pnl: float = 0.0
    label: Optional[str] = None
    wave_label: Optional[str] = None
    equity: Optional[float] = None
    count_fingerprint: Optional[str] = None
    at: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source, "symbol": self.symbol, "timeframe": self.timeframe,
            "position_id": self.position_id, "event": self.event, "label": self.label,
            "wave_label": self.wave_label, "side": self.side, "price": self.price,
            "quantity": self.quantity, "realized_pnl": self.realized_pnl,
            "position_realized_pnl": self.position_realized_pnl, "equity": self.equity,
            "count_fingerprint": self.count_fingerprint, "at": self.at,
        }


def _use_rest(database_url: Optional[str]) -> bool:
    """See analysis_store._use_rest - an explicit URL always wins, which is
    what keeps a test on its own file even on a deployed box."""
    return database_url is None and supabase_rest.configured()


def record(event: TradeEvent, database_url: Optional[str] = None) -> None:
    """Write one fill down. Never raises into the trading path: a journal
    that cannot be written is a lost record, and losing the TRADE as well
    because of it would be the worse failure."""
    if _use_rest(database_url):
        try:
            row = event.as_dict()
            row["at"] = event.at or int(time.time() * 1000)
            supabase_rest.insert("ai_trade_events", [row])
        except Exception:               # noqa: BLE001 - see docstring
            pass
        return
    try:
        with session_scope(_sessions(database_url)) as session:
            session.add(AiTradeEventRow(
                source=event.source, symbol=event.symbol, timeframe=event.timeframe,
                position_id=event.position_id, event=event.event, label=event.label,
                wave_label=event.wave_label, side=event.side, price=event.price,
                quantity=event.quantity, realized_pnl=event.realized_pnl,
                position_realized_pnl=event.position_realized_pnl, equity=event.equity,
                count_fingerprint=event.count_fingerprint,
                at=event.at or int(time.time() * 1000),
            ))
    except Exception:                       # noqa: BLE001 - see docstring
        return


def events_for(source: str, symbol: str, timeframe: Optional[str] = None,
               limit: int = MAX_EVENTS_RETURNED,
               database_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fills for one instrument, newest first."""
    if _use_rest(database_url):
        try:
            filters = {"source": source, "symbol": symbol}
            if timeframe:
                filters["timeframe"] = timeframe
            return supabase_rest.select("ai_trade_events", filters, order="at.desc",
                                        limit=max(1, min(limit, MAX_EVENTS_RETURNED)))
        except Exception:               # noqa: BLE001
            return []
    try:
        with session_scope(_sessions(database_url)) as session:
            query = select(AiTradeEventRow).where(AiTradeEventRow.source == source,
                                                  AiTradeEventRow.symbol == symbol)
            if timeframe:
                query = query.where(AiTradeEventRow.timeframe == timeframe)
            rows = session.execute(
                query.order_by(AiTradeEventRow.at.desc(), AiTradeEventRow.id.desc())
                .limit(max(1, min(limit, MAX_EVENTS_RETURNED)))
            ).scalars().all()
            return [TradeEvent(
                source=r.source, symbol=r.symbol, timeframe=r.timeframe,
                position_id=r.position_id, event=r.event, label=r.label,
                wave_label=r.wave_label, side=r.side, price=r.price, quantity=r.quantity,
                realized_pnl=r.realized_pnl, position_realized_pnl=r.position_realized_pnl,
                equity=r.equity, count_fingerprint=r.count_fingerprint, at=r.at,
            ).as_dict() for r in rows]
    except Exception:                       # noqa: BLE001
        return []


def summary_for(source: str, symbol: str, timeframe: Optional[str] = None,
                database_url: Optional[str] = None) -> Dict[str, Any]:
    """What the agent's counts on this instrument have actually done.

    Counted from the fills themselves rather than from any running total,
    so a restart cannot make the number drift. Take-profit legs and stops
    are counted separately because they are different exits, not because
    one means right and the other wrong - the runner leg exits on a
    trailing structural stop and is frequently the most profitable of the
    three, so `stops_hit` is a count of stop exits, never of losses.
    `wins`/`losses` are decided by the sign of the money."""
    rows = events_for(source, symbol, timeframe, limit=MAX_EVENTS_RETURNED,
                      database_url=database_url)
    fills = [r for r in rows if r["event"] != "ENTRY"]
    realized = sum(r["realized_pnl"] for r in fills)
    wins = sum(1 for r in fills if r["realized_pnl"] > 0)
    losses = sum(1 for r in fills if r["realized_pnl"] < 0)
    return {
        "trades_opened": sum(1 for r in rows if r["event"] == "ENTRY"),
        "fills": len(fills),
        "take_profits_hit": sum(1 for r in fills if r["event"] == "TP_HIT"),
        "stops_hit": sum(1 for r in fills if r["event"] == "STOP_LOSS"),
        "wins": wins,
        "losses": losses,
        "realized_pnl": round(realized, 4),
        "last_event_at": rows[0]["at"] if rows else None,
    }


def brief_line(summary: Dict[str, Any], symbol: str, timeframe: str) -> str:
    """One factual sentence for the analyst's opening brief, or "".

    Stated as record, never as steer. The agent is told what happened on
    this chart; it is not told what to conclude from it, and there is no
    "so do better this time" anywhere in it."""
    if not summary or not summary.get("trades_opened"):
        return ""
    pnl = summary["realized_pnl"]
    return (
        f"Record so far on {symbol} {timeframe}: {summary['trades_opened']} paper trade(s) were "
        f"opened from previous counts of this chart, {summary['take_profits_hit']} take-profit "
        f"leg(s) filled and {summary['stops_hit']} stop(s) hit, for {pnl:+g} realized. "
        "This is history, not an instruction: label what the chart shows now."
    )
