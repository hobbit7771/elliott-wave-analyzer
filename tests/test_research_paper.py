"""The PAPER terminal: persistence, recovery, and the states it reports.

Render Free stops a web service about fifteen minutes after the last
inbound request, so a restart is the NORMAL case here, not an edge case.
These tests are about what survives one and what must not.
"""

import pytest

from t3_engine.research import paper as pp
from t3_engine.research.portfolio import JournalRow, LONG, QUALITY_OK


def _book_frame(ms, mid=5.0, kind="snapshot"):
    half = 0.001
    return ("orderbook.50.INJUSDT",
            {"ts": ms, "type": kind,
             "data": {"u": ms,
                      "b": [[f"{mid - half:.4f}", "100"], [f"{mid - 2*half:.4f}", "200"]],
                      "a": [[f"{mid + half:.4f}", "100"], [f"{mid + 2*half:.4f}", "200"]]}},
            ms)


def _trade_frame(ms, mid=5.0, side="Buy", size=10.0):
    return ("publicTrade.INJUSDT",
            {"ts": ms, "type": "snapshot",
             "data": [{"T": ms, "p": f"{mid:.4f}", "v": str(size), "S": side}]},
            ms)


def _trader(**kwargs):
    kwargs.setdefault("persist", False)
    trader = pp.PaperTrader("INJUSDT", "order_flow_impulse", **kwargs)
    trader.start()
    return trader


# ---- status ---------------------------------------------------------------

def test_a_fresh_trader_is_recovering_and_refuses_to_enter():
    """A strategy that trades off a half-warm window is trading off
    noise."""
    trader = _trader()
    ms = 1_700_000_000_000
    trader.observe(*_book_frame(ms))
    assert trader.status.state == pp.RECOVERING
    assert trader.may_enter is False
    assert trader.status.warmup_remaining_ms > 0


def test_it_reaches_running_only_after_the_warmup_has_elapsed():
    trader = _trader()
    ms = 1_700_000_000_000
    for i in range(200):
        ms += 300
        trader.observe(*_book_frame(ms, kind="snapshot" if i == 0 else "delta"))
    assert ms - 1_700_000_000_000 > pp.WARMUP_MS
    assert trader.status.state == pp.RUNNING
    assert trader.may_enter is True


def test_a_stale_feed_is_reported_as_stale_and_blocks_entries():
    """An old frame must never look alive on the screen."""
    trader = _trader()
    ms = 1_700_000_000_000
    for i in range(200):
        ms += 300
        trader.observe(*_book_frame(ms, kind="snapshot" if i == 0 else "delta"))
    assert trader.status.state == pp.RUNNING

    ms += 120_000                       # two minutes of nothing, then a trade
    trader.observe(*_trade_frame(ms))
    assert trader.status.state == pp.FEED_STALE
    assert trader.may_enter is False
    assert trader.status.reason


def test_the_heartbeat_ages_so_a_dead_process_cannot_look_live():
    trader = _trader()
    trader.observe(*_book_frame(1_700_000_000_000))
    report = trader.report()
    assert report["status"]["heartbeat_age_seconds"] is not None
    assert report["status"]["last_event_ms"] == 1_700_000_000_000


# ---- persistence ----------------------------------------------------------

def test_a_closed_trade_is_written_once_and_upserted_on_a_retry():
    """The difference between a journal and a story about a journal."""
    written = []

    def fake_save(rows):
        written.extend(rows)

    trader = pp.PaperTrader("INJUSDT", "order_flow_impulse", persist=True)
    trader.persist = True
    original_save, original_load = pp._save_trades, pp._load_trades
    pp._save_trades = fake_save
    pp._load_trades = lambda symbol, limit=2_000: []
    try:
        trader.start()
        row = JournalRow(
            trade_id="t1", strategy_version="v", config_hash="c", signal_id="s",
            symbol="INJUSDT", direction=LONG, qty=1.0, entry_at_ms=1,
            entry_price=5.0, entry_liquidity="taker", entry_order_id="o",
            entry_fill_ids=["f"], exit_at_ms=2, exit_price=5.1,
            exit_reason="TARGET", net_pnl=0.1, gross_pnl=0.11, fees=0.01)
        trader.runner.portfolio.journal.append(row)
        trader._flush_journal()
        trader._flush_journal()             # a second pass must not re-write
    finally:
        pp._save_trades, pp._load_trades = original_save, original_load

    assert len(written) == 1
    assert written[0]["trade_id"].endswith(":t1")
    assert written[0]["net_pnl"] == 0.1
    assert written[0]["exit_reason"] == "TARGET"


def test_a_storage_failure_is_reported_rather_than_swallowed():
    def boom(rows):
        raise RuntimeError("supabase timed out")

    trader = pp.PaperTrader("INJUSDT", "order_flow_impulse", persist=True)
    original_save, original_load = pp._save_trades, pp._load_trades
    pp._save_trades = boom
    pp._load_trades = lambda symbol, limit=2_000: []
    try:
        trader.start()
        trader.runner.portfolio.journal.append(JournalRow(
            trade_id="t1", strategy_version="v", config_hash="c", signal_id="s",
            symbol="INJUSDT", direction=LONG, qty=1.0, entry_at_ms=1,
            entry_price=5.0, entry_liquidity="taker", entry_order_id="o",
            entry_fill_ids=["f"], exit_at_ms=2, exit_price=5.1,
            exit_reason="TARGET", net_pnl=0.1))
        trader._flush_journal()
    finally:
        pp._save_trades, pp._load_trades = original_save, original_load

    assert trader.status.state == pp.STORAGE_DEGRADED
    assert "timed out" in trader.status.reason
    assert trader.status.storage_failures == 1


def test_the_journal_survives_a_restart_and_the_pnl_comes_back_with_it():
    stored = [{
        "trade_id": "s1:t1", "session_id": "s1", "symbol": "INJUSDT",
        "strategy_version": "order_flow_impulse@1.0.0", "config_hash": "abc",
        "signal_id": "sig", "episode_id": "e1", "direction": "long", "qty": 2.0,
        "entry_at_ms": 1_000, "entry_price": 5.0, "entry_liquidity": "taker",
        "exit_at_ms": 2_000, "exit_price": 5.2, "exit_liquidity": "taker",
        "exit_reason": "TARGET", "fees": 0.01, "funding": 0.0,
        "gross_pnl": 0.4, "net_pnl": 0.39, "quality": "OK",
        "fills": {"entry": ["f1"], "exit": ["f2"]}, "notes": "",
    }]
    original_load = pp._load_trades
    pp._load_trades = lambda symbol, limit=2_000: list(stored)
    try:
        trader = pp.PaperTrader("INJUSDT", "order_flow_impulse", persist=True)
        trader.start()
    finally:
        pp._load_trades = original_load

    assert trader.status.recovered_trades == 1
    report = trader.report()
    assert report["trades"] == 1
    assert report["net_pnl"] == pytest.approx(0.39)


def test_an_open_position_from_a_dead_process_does_not_come_back_open():
    """The market moved while nothing was managing it. Claiming the
    position survived would invent a position nobody held."""
    original_load = pp._load_trades
    pp._load_trades = lambda symbol, limit=2_000: []
    try:
        trader = pp.PaperTrader("INJUSDT", "order_flow_impulse", persist=True)
        trader.start()
    finally:
        pp._load_trades = original_load
    assert trader.runner.portfolio.positions == {}
    assert trader.report()["open_position"] is None


def test_a_restart_cannot_invent_fills_inside_the_downtime():
    """Whatever the price did while the process was dead, no stop and no
    target may be claimed inside that hole."""
    trader = _trader()
    ms = 1_700_000_000_000
    for i in range(200):
        ms += 300
        trader.observe(*_book_frame(ms, kind="snapshot" if i == 0 else "delta"))
    fills_before = len(trader.runner.sim.fills)

    # Two hours pass with the process dead, then the feed returns far away.
    ms += 2 * 60 * 60 * 1000
    trader.observe(*_book_frame(ms, mid=4.0))
    assert len(trader.runner.sim.fills) == fills_before
    assert trader.runner.gaps == 1
    assert trader.may_enter is False


# ---- the tap fan ----------------------------------------------------------

def test_one_observer_raising_never_stops_the_others():
    """A paper trader with a bug must not silently stop the recording."""
    from t3_engine.research import TapFan

    seen = []
    fan = TapFan()
    fan.add(lambda t, m, r: (_ for _ in ()).throw(ValueError("boom")))
    fan.add(lambda t, m, r: seen.append(t))
    fan("publicTrade.INJUSDT", {"ts": 1, "data": []}, 1)
    assert seen == ["publicTrade.INJUSDT"]
    assert fan.errors == 1


def test_the_same_observer_is_not_added_twice():
    from t3_engine.research import TapFan

    seen = []
    fan = TapFan()
    observer = lambda t, m, r: seen.append(t)      # noqa: E731
    fan.add(observer)
    fan.add(observer)
    fan("publicTrade.INJUSDT", {"ts": 1, "data": []}, 1)
    assert len(seen) == 1 and len(fan) == 1


# ---- isolation ------------------------------------------------------------

def test_the_research_package_cannot_reach_an_exchange_credential():
    """Same rule as the external API, for the same reason."""
    import pathlib

    root = pathlib.Path(pp.__file__).parent
    banned = ("api_key", "apiKey", "api_secret", "place_order", "create_order",
              "submit_order", "ccxt", "pybit")
    for path in root.glob("*.py"):
        text = path.read_text()
        for needle in banned:
            assert needle not in text, f"{path.name} mentions {needle}"
