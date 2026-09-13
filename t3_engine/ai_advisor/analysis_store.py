"""Saved analyses, so the same conclusions are not paid for twice.

A full analyst run is a dozen model calls over a whole history. Running
four timeframes from scratch on every request buys the same answers again
at full price, and most of the time nothing has changed: a 4h chart
produces one new candle every four hours, so an analysis of it is good for
hours.

What decides freshness is the DATA, not a clock. An analysis is stale when
candles have arrived that it never saw - `last_candle_time` records the
newest one it did. A chart that has not moved has nothing new to say, and a
timer would throw the work away anyway.

The store is deliberately dumb: it holds the analyst's own result verbatim
as JSON and never interprets it. Anything that needs to understand a saved
analysis reads it through the same code that produced it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select

from t3_engine.database.models import AnalysisCacheRow
from t3_engine.database import supabase_rest
from t3_engine.database.session import init_db, make_session_factory, session_scope

DEFAULT_DATABASE_URL = os.getenv("T3_DATABASE_URL", "sqlite:///./t3_engine.db")

# How many entries to keep per (source, symbol). Enough for every timeframe
# the dashboard offers, several times over, without letting a long-running
# instance accumulate forever.
MAX_ENTRIES_PER_SERIES = 40

_factory = None


def _sessions(database_url: Optional[str] = None):
    global _factory
    if _factory is None or database_url:
        engine = init_db(database_url or DEFAULT_DATABASE_URL)
        factory = make_session_factory(engine)
        if database_url:
            return factory          # an explicit URL is not cached globally
        _factory = factory
    return _factory


@dataclass
class CachedAnalysis:
    source: str
    symbol: str
    timeframe: str
    last_candle_time: int
    candle_count: int
    model: str
    created_at: int
    payload: Dict[str, Any]

    def is_fresh_for(self, newest_candle_time: int) -> bool:
        """Fresh while no candle newer than the one it analysed exists.

        Deliberately exact rather than a tolerance: one new 4h candle can
        end a wave, and "close enough" is how a stale count survives the
        bar that invalidated it."""
        return self.last_candle_time >= newest_candle_time


def _rest_row_to_cached(row: Dict[str, Any]) -> Optional[CachedAnalysis]:
    try:
        payload = json.loads(row["payload"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None         # a corrupt entry is worth less than no entry
    return CachedAnalysis(
        source=row.get("source", ""), symbol=row.get("symbol", ""),
        timeframe=row.get("timeframe", ""),
        last_candle_time=int(row.get("last_candle_time") or 0),
        candle_count=int(row.get("candle_count") or 0),
        model=row.get("model") or "", created_at=int(row.get("created_at") or 0),
        payload=payload,
    )


def _use_rest(database_url: Optional[str]) -> bool:
    """Supabase REST is used only when it is configured AND the caller has
    not named a database of its own. An explicit URL always wins, which is
    what keeps tests on their own SQLite file even on a deployed box."""
    return database_url is None and supabase_rest.configured()


def save(source: str, symbol: str, timeframe: str, last_candle_time: int,
         candle_count: int, payload: Dict[str, Any], model: str = "",
         database_url: Optional[str] = None) -> None:
    """Replace whatever was stored for this series. One analysis per
    (source, symbol, timeframe): keeping older ones would only invite
    reading a superseded count."""
    if _use_rest(database_url):
        key = {"source": source, "symbol": symbol, "timeframe": timeframe}
        supabase_rest.delete("analysis_cache", key)
        supabase_rest.insert("analysis_cache", [{
            **key, "last_candle_time": last_candle_time, "candle_count": candle_count,
            "model": model, "created_at": int(time.time()), "payload": json.dumps(payload),
        }])
        return
    factory = _sessions(database_url)
    with session_scope(factory) as session:
        session.execute(delete(AnalysisCacheRow).where(
            AnalysisCacheRow.source == source,
            AnalysisCacheRow.symbol == symbol,
            AnalysisCacheRow.timeframe == timeframe,
        ))
        session.add(AnalysisCacheRow(
            source=source, symbol=symbol, timeframe=timeframe,
            last_candle_time=int(last_candle_time), candle_count=int(candle_count),
            model=model or "", created_at=int(time.time()),
            payload=json.dumps(payload, default=str),
        ))
        _prune(session, source, symbol)


def _prune(session, source: str, symbol: str) -> None:
    rows = session.execute(
        select(AnalysisCacheRow)
        .where(AnalysisCacheRow.source == source, AnalysisCacheRow.symbol == symbol)
        .order_by(AnalysisCacheRow.created_at.desc())
    ).scalars().all()
    for row in rows[MAX_ENTRIES_PER_SERIES:]:
        session.delete(row)


def load(source: str, symbol: str, timeframe: str,
         database_url: Optional[str] = None) -> Optional[CachedAnalysis]:
    if _use_rest(database_url):
        rows = supabase_rest.select("analysis_cache",
                                    {"source": source, "symbol": symbol, "timeframe": timeframe},
                                    order="created_at.desc", limit=1)
        return _rest_row_to_cached(rows[0]) if rows else None
    factory = _sessions(database_url)
    with session_scope(factory) as session:
        row = session.execute(
            select(AnalysisCacheRow).where(
                AnalysisCacheRow.source == source,
                AnalysisCacheRow.symbol == symbol,
                AnalysisCacheRow.timeframe == timeframe,
            ).order_by(AnalysisCacheRow.created_at.desc())
        ).scalars().first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload)
        except json.JSONDecodeError:
            # A corrupt entry is worth less than no entry: it would be
            # rendered as a real analysis.
            return None
        return CachedAnalysis(
            source=row.source, symbol=row.symbol, timeframe=row.timeframe,
            last_candle_time=row.last_candle_time, candle_count=row.candle_count,
            model=row.model or "", created_at=row.created_at, payload=payload,
        )


def list_for(source: str, symbol: str,
             database_url: Optional[str] = None) -> List[CachedAnalysis]:
    """Every saved analysis for one instrument, newest first.

    This is what makes the analyst tab accumulate rather than replace: a
    run on 4h does not erase the 1h count that was paid for yesterday, and
    the tab can show every timeframe that has ever been analysed instead
    of only the one just requested."""
    if _use_rest(database_url):
        rows = supabase_rest.select("analysis_cache", {"source": source, "symbol": symbol},
                                    order="created_at.desc", limit=MAX_ENTRIES_PER_SERIES)
        return [c for c in (_rest_row_to_cached(r) for r in rows) if c is not None]
    factory = _sessions(database_url)
    out: List[CachedAnalysis] = []
    with session_scope(factory) as session:
        rows: List[AnalysisCacheRow] = session.execute(
            select(AnalysisCacheRow).where(AnalysisCacheRow.source == source,
                                           AnalysisCacheRow.symbol == symbol)
            .order_by(AnalysisCacheRow.created_at.desc())
        ).scalars().all()
        for row in rows:
            try:
                payload = json.loads(row.payload)
            except json.JSONDecodeError:
                continue        # a corrupt entry is worth less than no entry
            out.append(CachedAnalysis(
                source=row.source, symbol=row.symbol, timeframe=row.timeframe,
                last_candle_time=row.last_candle_time, candle_count=row.candle_count,
                model=row.model or "", created_at=row.created_at, payload=payload,
            ))
    return out


def clear(source: str, symbol: str, database_url: Optional[str] = None) -> int:
    """Drop every saved analysis for one series. The escape hatch for "I
    want this recomputed regardless"."""
    if _use_rest(database_url):
        return supabase_rest.delete("analysis_cache", {"source": source, "symbol": symbol})
    factory = _sessions(database_url)
    with session_scope(factory) as session:
        rows: List[AnalysisCacheRow] = session.execute(
            select(AnalysisCacheRow).where(AnalysisCacheRow.source == source,
                                           AnalysisCacheRow.symbol == symbol)
        ).scalars().all()
        for row in rows:
            session.delete(row)
        return len(rows)
