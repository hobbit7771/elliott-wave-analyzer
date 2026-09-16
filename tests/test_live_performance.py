"""The P&L surface: where the algorithm trades, and what it made.

The defect these pin: both books were running and neither had a surface.
"я не вижу где алгоритм торгует, нет ни вкладки доходности" was correct -
the paper engine's equity, positions and fills existed only inside the
process, and a paper engine whose P&L you cannot see is indistinguishable
from one that is not trading at all.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("LEAD_ENGINE_ENABLED", "true")

from fastapi.testclient import TestClient      # noqa: E402

from t3_engine.common.models import (          # noqa: E402
    Candle, Position, TakeProfitLeg, TradeSide, WaveLabel,
)
from t3_engine.dashboard import server as dashboard   # noqa: E402

client = TestClient(dashboard.app)


@pytest.fixture
def paper_engine():
    """One LiveTradingEngine registered exactly as autostart registers it,
    removed afterwards so no test leaks a running book into another."""
    from t3_engine.pipeline.live_loop import LiveTradingEngine

    engine = LiveTradingEngine(symbol="TESTUSDT", initial_equity=10_000.0)
    dashboard._live_engines["TESTUSDT"] = engine
    dashboard._live_started_at["TESTUSDT"] = 1_700_000_000_000
    try:
        yield engine
    finally:
        dashboard._live_engines.pop("TESTUSDT", None)
        dashboard._live_started_at.pop("TESTUSDT", None)
        dashboard._live_errors.pop("TESTUSDT", None)


def _position(entry: float, quantity: float, side=TradeSide.LONG) -> Position:
    return Position(
        position_id="p1", symbol="TESTUSDT", side=side,
        entry_price=entry, quantity=quantity, initial_quantity=quantity,
        stop_loss=entry * 0.99,
        take_profits=[TakeProfitLeg(price=entry * 1.02, fraction=0.5, label="TP1")],
        opened_at=1_700_000_000_000, wave_label=WaveLabel.W3,
        signal_id="s1", risk_amount=100.0,
    )


def test_the_endpoint_reports_both_books_and_never_adds_them(paper_engine):
    payload = client.get("/api/live/performance").json()
    assert "paper" in payload and "lead" in payload
    assert "TESTUSDT" in payload["paper"]
    # No combined total anywhere: they are different strategies on
    # different horizons and one number would hide which is working.
    assert "total_pnl" not in payload
    assert "combined" not in payload
    assert "PAPER ONLY" in payload["disclaimer"]


def test_a_running_engine_reports_its_equity_and_mode(paper_engine):
    state = client.get("/api/live/performance").json()["paper"]["TESTUSDT"]
    assert state["mode"] == "paper"
    assert state["live_source"] == "bybit"
    assert state["open_positions"] == []
    assert state["entries"] == 0 and state["exits"] == 0
    for timeframe in paper_engine.trading_timeframes:
        assert timeframe.value in state["timeframes"]
        assert state["timeframes"][timeframe.value]["equity"] == pytest.approx(10_000.0)


def test_capital_is_not_summed_across_timeframes(paper_engine):
    """Five books of 10 000 are not 50 000 of capital.

    Every timeframe runs the same strategy off the same nominal equity,
    so adding them invents money that was never allocated - and on five
    timeframes it inflates the denominator of every percentage by five.
    The per-timeframe figure is stated once; what IS additive is the P&L.
    """
    state = client.get("/api/live/performance").json()["paper"]["TESTUSDT"]
    assert state["equity_per_timeframe"] == pytest.approx(10_000.0)
    assert "equity" not in state, "a portfolio equity would be fiction"
    assert "initial_equity" not in state
    assert state["pnl"] == pytest.approx(state["realized_pnl"]
                                         + state["unrealized_pnl"])


def test_an_open_position_is_marked_to_the_live_price_not_to_its_entry(paper_engine):
    """A position reported at its entry price is a position that is never
    losing. The mark has to be the freshest price the session has seen."""
    timeframe = list(paper_engine.trading_timeframes)[0]
    backtest = paper_engine.engines[timeframe]
    position = _position(entry=100.0, quantity=2.0)
    backtest.position_manager.positions[position.position_id] = position

    # The freshest price is 110, and it is in the FORMING bar - the last
    # closed candle still says 100.
    paper_engine.history[timeframe] = [
        Candle(timeframe=timeframe, open_time=1, close_time=2,
               open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0, closed=True)
    ]

    class _Forming:
        close = 110.0

    original = paper_engine.candle_builder.current_candle
    paper_engine.candle_builder.current_candle = lambda tf: _Forming()
    try:
        state = client.get("/api/live/performance").json()["paper"]["TESTUSDT"]
    finally:
        paper_engine.candle_builder.current_candle = original

    assert state["mark"] == 110.0
    rows = state["open_positions"]
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 100.0
    assert rows[0]["unrealized_pnl"] == pytest.approx(20.0)   # (110-100) * 2
    assert rows[0]["timeframe"] == timeframe.value


def test_a_short_is_marked_the_other_way(paper_engine):
    timeframe = list(paper_engine.trading_timeframes)[0]
    backtest = paper_engine.engines[timeframe]
    position = _position(entry=100.0, quantity=2.0, side=TradeSide.SHORT)
    backtest.position_manager.positions[position.position_id] = position

    class _Forming:
        close = 90.0

    original = paper_engine.candle_builder.current_candle
    paper_engine.candle_builder.current_candle = lambda tf: _Forming()
    try:
        state = client.get("/api/live/performance").json()["paper"]["TESTUSDT"]
    finally:
        paper_engine.candle_builder.current_candle = original

    assert state["open_positions"][0]["unrealized_pnl"] == pytest.approx(20.0)


def test_fills_reach_the_journal_newest_first(paper_engine):
    paper_engine.fills = [
        {"event": "ENTRY", "label": None, "price": 100.0, "timeframe": "5m",
         "position_id": "p1", "wave_label": "3", "side": "LONG", "quantity": 2.0,
         "position_realized_pnl": 0.0, "at": 1_700_000_000_000, "closed": False},
        {"event": "TP_HIT", "label": "TP1", "price": 102.0, "timeframe": "5m",
         "position_id": "p1", "wave_label": "3", "side": "LONG", "quantity": 1.0,
         "position_realized_pnl": 2.0, "at": 1_700_000_060_000, "closed": False},
        {"event": "STOP_LOSS", "label": None, "price": 99.0, "timeframe": "5m",
         "position_id": "p1", "wave_label": "3", "side": "LONG", "quantity": 1.0,
         "position_realized_pnl": 1.0, "at": 1_700_000_120_000, "closed": True},
    ]
    state = client.get("/api/live/performance").json()["paper"]["TESTUSDT"]
    assert state["entries"] == 1
    assert state["exits"] == 2
    assert state["take_profits"] == 1
    assert state["stops"] == 1
    assert [f["event"] for f in state["fills"]] == ["STOP_LOSS", "TP_HIT", "ENTRY"]
    # The fill journal is a record of EVENTS. Realized P&L is not read
    # back out of it - it comes from the position manager's own closed
    # positions, below - because replaying a journal to recompute money
    # is how a restart makes the two disagree.
    assert state["realized_pnl"] == 0.0, "no position has actually closed"


def test_realized_pnl_comes_from_closed_positions_not_from_the_journal(paper_engine):
    """The journal says what happened; the position manager says what it
    was worth. Deriving money from the event log means a dropped or
    replayed event changes the P&L, which is exactly the drift this
    avoids."""
    timeframe = list(paper_engine.trading_timeframes)[0]
    manager = paper_engine.engines[timeframe].position_manager
    closed = _position(entry=100.0, quantity=0.0)
    closed.closed = True
    closed.realized_pnl = 7.5
    manager.closed_positions.append(closed)

    state = client.get("/api/live/performance").json()["paper"]["TESTUSDT"]
    assert state["realized_pnl"] == pytest.approx(7.5)
    assert state["timeframes"][timeframe.value]["realized_pnl"] == pytest.approx(7.5)
    assert state["timeframes"][timeframe.value]["closed_positions"] == 1


def test_one_symbol_can_be_asked_for(paper_engine):
    payload = client.get("/api/live/performance", params={"symbol": "NOPEUSDT"}).json()
    assert payload["paper"] == {}
    payload = client.get("/api/live/performance", params={"symbol": "TESTUSDT"}).json()
    assert list(payload["paper"]) == ["TESTUSDT"]


def test_nothing_running_is_an_answer_not_an_error():
    payload = client.get("/api/live/performance").json()
    assert payload["paper"] == {} or isinstance(payload["paper"], dict)
    assert isinstance(payload["autostart"], list)
    assert payload["server_time"] > 0


def test_the_endpoint_cannot_trade():
    """Read-only, like everything else that faces outward: it reports what
    the paper books did and holds no path to an exchange."""
    from pathlib import Path
    import re

    source = Path("t3_engine/dashboard/server.py").read_text()
    match = re.search(r"def live_performance\(.*?\n(?=@app\.)", source, re.S)
    assert match, "live_performance not found"
    body = match.group(0)
    for forbidden in ("place_order", "create_order", "api_secret", "requests.post"):
        assert forbidden not in body, forbidden


def test_the_tab_exists_and_is_wired():
    """A surface nobody can reach is the defect this fixes, so the wiring
    is asserted rather than assumed: the button, the panel, the script,
    and the show/hide that stops it polling when hidden."""
    page = client.get("/static/index.html")
    assert page.status_code == 200
    assert 'data-tab="pnl"' in page.text
    assert 'id="tab-pnl"' in page.text
    assert "pnl: 'tab-pnl'" in page.text
    assert "/static/pnl.js" in page.text
    assert "PnlTab.show()" in page.text and "PnlTab.hide()" in page.text

    script = client.get("/static/pnl.js")
    assert script.status_code == 200
    assert "/api/live/performance" in script.text
