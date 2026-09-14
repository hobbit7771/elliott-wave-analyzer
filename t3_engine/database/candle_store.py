"""The candle series an analysis was actually run on, kept alongside it.

The analysis cache stores a COUNT - which swings are waves 1 through 5.
It does not store the chart those waves sit on, so a saved count cannot be
re-examined, re-validated against different rules, or compared with a
second opinion without re-fetching the candles from the exchange and
hoping they are the same ones. "The same ones" is not a given: a later
fetch returns a different window, and a count checked against a different
window is a different claim.

So the series travels with the analysis. Three things fall out of that,
all of them wanted:

  - A count can be replayed exactly, months later, against the bars it was
    made on.
  - A second analyst - another model, or a person - can work the identical
    chart rather than a similar one.
  - Anything that can read the database can read the market data the model
    saw, without needing a route to the exchange itself. That is the case
    that prompted this: the exchange is reachable from the deployment and
    not from everywhere else.

Keyed on (symbol, timeframe), one series each, replaced wholesale - the
same shape as the analysis cache, for the same reason: two overlapping
windows of the same instrument invite reading the wrong one.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import delete, select

from t3_engine.ai_advisor import analysis_store
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe
from t3_engine.database import supabase_rest
from t3_engine.database.models import CandleRow
from t3_engine.database.session import init_db, make_session_factory, session_scope

_factory = None

# PostgREST takes a whole array in one POST, but a 10k-candle history is a
# multi-megabyte body. Chunked so a long series is several ordinary
# requests rather than one that may be refused for its size.
INSERT_CHUNK = 500

# Nothing reads more than a full analyst window, and an unbounded read of
# a table that accumulates would be a slow surprise.
MAX_CANDLES_RETURNED = 10_000


def _sessions(database_url: Optional[str] = None):
    global _factory
    if _factory is None or database_url:
        engine = init_db(database_url or analysis_store.DEFAULT_DATABASE_URL)
        factory = make_session_factory(engine)
        if database_url:
            return factory
        _factory = factory
    return _factory


def _use_rest(database_url: Optional[str]) -> bool:
    return database_url is None and supabase_rest.configured()


def _row(symbol: str, timeframe: str, candle: Candle) -> dict:
    return {
        "symbol": symbol, "timeframe": timeframe,
        "open_time": candle.open_time, "close_time": candle.close_time,
        "open": candle.open, "high": candle.high, "low": candle.low,
        "close": candle.close, "volume": candle.volume,
        "taker_buy_volume": candle.taker_buy_volume, "trades": candle.trades,
    }


def save(symbol: str, timeframe: str, candles: List[Candle],
         database_url: Optional[str] = None) -> int:
    """Replace the stored series for this instrument. Returns rows written.

    Never raises into the caller: this runs beside an analysis, and losing
    the ANALYSIS because its candles could not be filed would be a much
    worse trade than losing the copy of the candles."""
    if not candles:
        return 0
    rows = [_row(symbol, timeframe, candle) for candle in candles]
    try:
        if _use_rest(database_url):
            supabase_rest.delete("candles", {"symbol": symbol, "timeframe": timeframe})
            for start in range(0, len(rows), INSERT_CHUNK):
                supabase_rest.insert("candles", rows[start:start + INSERT_CHUNK])
            return len(rows)

        with session_scope(_sessions(database_url)) as session:
            session.execute(delete(CandleRow).where(CandleRow.symbol == symbol,
                                                    CandleRow.timeframe == timeframe))
            session.add_all([CandleRow(**row) for row in rows])
        return len(rows)
    except Exception:                       # noqa: BLE001 - see docstring
        return 0


def load(symbol: str, timeframe: str, limit: int = MAX_CANDLES_RETURNED,
         database_url: Optional[str] = None) -> List[Candle]:
    """The stored series, oldest first - the order every consumer of a
    candle list in this codebase assumes."""
    try:
        degree = Timeframe(timeframe)
    except ValueError:
        return []
    capped = max(1, min(int(limit), MAX_CANDLES_RETURNED))
    try:
        if _use_rest(database_url):
            rows = supabase_rest.select("candles", {"symbol": symbol, "timeframe": timeframe},
                                        order="open_time.asc", limit=capped)
            return [Candle(
                timeframe=degree, open_time=int(r["open_time"]), close_time=int(r["close_time"]),
                open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                close=float(r["close"]), volume=float(r.get("volume") or 0.0),
                taker_buy_volume=float(r.get("taker_buy_volume") or 0.0),
                trades=int(r.get("trades") or 0),
            ) for r in rows]

        with session_scope(_sessions(database_url)) as session:
            rows = session.execute(
                select(CandleRow)
                .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe)
                .order_by(CandleRow.open_time.asc()).limit(capped)
            ).scalars().all()
            return [Candle(
                timeframe=degree, open_time=r.open_time, close_time=r.close_time,
                open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
                taker_buy_volume=r.taker_buy_volume, trades=r.trades,
            ) for r in rows]
    except Exception:                       # noqa: BLE001
        return []
