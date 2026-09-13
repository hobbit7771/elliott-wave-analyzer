"""The record of what the agent's counts actually did.

Written because of a real screen: a live position with two of its four
take-profit legs filled showed as "open positions: 1, closed trades: 0"
and nothing else. The fills were real and nothing was listening.
"""

import pytest

from t3_engine.ai_advisor import analysis_store, trade_journal
from t3_engine.ai_advisor.trade_journal import TradeEvent
from t3_engine.database.session import is_durable, normalize_database_url


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'journal.db'}"
    monkeypatch.setattr(analysis_store, "DEFAULT_DATABASE_URL", url)
    monkeypatch.setattr(analysis_store, "_factory", None)
    monkeypatch.setattr(trade_journal, "_factory", None)
    return url


def _event(**kwargs):
    base = dict(source="bybit", symbol="INJUSDT", timeframe="5m", position_id="p1",
                event="TP_HIT", side="LONG", price=6.19, quantity=1.0,
                realized_pnl=4.0, position_realized_pnl=4.0, label="TP1",
                wave_label="3", count_fingerprint="fp-1", at=1_700_000_000_000)
    base.update(kwargs)
    return TradeEvent(**base)


def test_a_take_profit_leg_is_recorded_and_readable(store):
    trade_journal.record(_event(), database_url=store)
    rows = trade_journal.events_for("bybit", "INJUSDT", "5m", database_url=store)
    assert len(rows) == 1
    assert rows[0]["event"] == "TP_HIT" and rows[0]["label"] == "TP1"
    assert rows[0]["count_fingerprint"] == "fp-1"


def test_the_summary_counts_targets_and_stops_separately(store):
    """They answer different questions: how often a target was reached,
    and how often the count was wrong."""
    trade_journal.record(_event(event="ENTRY", label=None, realized_pnl=0.0,
                                position_realized_pnl=0.0), database_url=store)
    trade_journal.record(_event(label="TP1", realized_pnl=4.0), database_url=store)
    trade_journal.record(_event(label="TP2", realized_pnl=6.0, position_id="p1"),
                         database_url=store)
    trade_journal.record(_event(event="STOP_LOSS", label=None, realized_pnl=-3.0,
                                position_id="p2"), database_url=store)

    summary = trade_journal.summary_for("bybit", "INJUSDT", "5m", database_url=store)
    assert summary["trades_opened"] == 1
    assert summary["take_profits_hit"] == 2
    assert summary["stops_hit"] == 1
    assert summary["wins"] == 2 and summary["losses"] == 1
    assert summary["realized_pnl"] == 7.0


def test_timeframes_are_kept_apart(store):
    trade_journal.record(_event(timeframe="5m"), database_url=store)
    trade_journal.record(_event(timeframe="4h", realized_pnl=10.0), database_url=store)
    assert trade_journal.summary_for("bybit", "INJUSDT", "5m",
                                     database_url=store)["realized_pnl"] == 4.0
    assert trade_journal.summary_for("bybit", "INJUSDT", "4h",
                                     database_url=store)["realized_pnl"] == 10.0
    # ...and asking without one sums the instrument
    assert trade_journal.summary_for("bybit", "INJUSDT",
                                     database_url=store)["realized_pnl"] == 14.0


def test_an_unrecorded_instrument_summarises_to_nothing_not_an_error(store):
    summary = trade_journal.summary_for("bybit", "NOSUCHUSDT", database_url=store)
    assert summary["trades_opened"] == 0 and summary["realized_pnl"] == 0


def test_the_brief_line_states_the_record_and_gives_no_instruction(store):
    trade_journal.record(_event(event="ENTRY", realized_pnl=0.0), database_url=store)
    trade_journal.record(_event(label="TP1", realized_pnl=4.0), database_url=store)
    summary = trade_journal.summary_for("bybit", "INJUSDT", "5m", database_url=store)
    line = trade_journal.brief_line(summary, "INJUSDT", "5m")

    assert "1 paper trade(s)" in line and "+4" in line
    # It must NOT tell the model what to conclude - that is how a model is
    # talked into fitting its next answer to the last result.
    lowered = line.lower()
    for steer in ("try", "instead", "avoid", "better", "should", "improve"):
        assert steer not in lowered
    assert "history, not an instruction" in lowered


def test_no_record_means_no_line_at_all(store):
    assert trade_journal.brief_line({}, "X", "5m") == ""
    assert trade_journal.brief_line({"trades_opened": 0}, "X", "5m") == ""


def test_a_journal_that_cannot_be_written_does_not_break_trading(monkeypatch):
    """Losing a record is bad; losing the TRADE because the record failed
    would be worse."""
    def explode(*a, **k):
        raise RuntimeError("database is gone")
    monkeypatch.setattr(trade_journal, "_sessions", explode)
    trade_journal.record(_event())              # must not raise
    assert trade_journal.events_for("bybit", "INJUSDT") == []


# ---- durability of the storage itself ----------------------------------

def test_a_sqlite_file_is_not_durable_and_postgres_is():
    """The default SQLite file lives on the container filesystem, which the
    host replaces on every deploy - which is exactly why every labelled
    chart was being erased by the next push."""
    assert is_durable("sqlite:///./t3_engine.db") is False
    assert is_durable("postgres://user:pw@host/db") is True


def test_provider_postgres_urls_are_accepted_as_pasted():
    """Render and Heroku print `postgres://`, which SQLAlchemy 2 refuses.
    Pasting the provider's own string must work rather than be a trap."""
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
