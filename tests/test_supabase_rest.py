"""Storage over Supabase's REST API.

This exists because the credential that is actually obtainable from a
Supabase project is the service key, not the database password: Supabase
shows the password exactly once at creation and never again, and a new
free project's direct host is IPv6-only, which the hosting this app runs
on cannot reach. PostgREST answers over ordinary IPv4 HTTPS with the key.
"""

import json

import httpx
import pytest

from t3_engine.ai_advisor import analysis_store, trade_journal
from t3_engine.ai_advisor.trade_journal import TradeEvent
from t3_engine.database import supabase_rest


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv(supabase_rest.URL_ENV, "https://project.supabase.co")
    monkeypatch.setenv(supabase_rest.KEY_ENV, "sb_secret_test")
    return True


class FakeRest:
    """A stand-in PostgREST: records what was asked and answers from a
    per-table list, so the URL and headers this layer builds are asserted
    rather than assumed."""

    def __init__(self, tables=None):
        self.tables = tables or {}
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        self.requests.append({
            "method": request.method, "table": table,
            "params": dict(request.url.params), "url": str(request.url),
            "apikey": request.headers.get("apikey"),
            "auth": request.headers.get("Authorization"),
            "body": json.loads(request.content) if request.content else None,
        })
        if request.method == "POST":
            self.tables.setdefault(table, []).extend(json.loads(request.content))
            return httpx.Response(201, json=json.loads(request.content))
        if request.method == "DELETE":
            removed = self.tables.get(table, [])
            self.tables[table] = []
            return httpx.Response(200, json=removed)
        return httpx.Response(200, json=self.tables.get(table, []))

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_the_secret_key_is_sent_on_every_call(configured):
    fake = FakeRest({"analysis_cache": []})
    supabase_rest.select("analysis_cache", {"symbol": "INJUSDT"}, client=fake.client())
    sent = fake.requests[0]
    assert sent["apikey"] == "sb_secret_test"
    assert sent["auth"] == "Bearer sb_secret_test"
    # equality filters are PostgREST's eq. syntax
    assert sent["params"]["symbol"] == "eq.INJUSDT"


def test_the_project_url_is_accepted_with_or_without_the_api_path(monkeypatch, configured):
    fake = FakeRest({"analysis_cache": []})
    supabase_rest.select("analysis_cache", client=fake.client())
    assert fake.requests[0]["url"].startswith("https://project.supabase.co/rest/v1/analysis_cache")

    monkeypatch.setenv(supabase_rest.URL_ENV, "https://project.supabase.co/rest/v1/")
    fake2 = FakeRest({"analysis_cache": []})
    supabase_rest.select("analysis_cache", client=fake2.client())
    assert fake2.requests[0]["url"].startswith("https://project.supabase.co/rest/v1/analysis_cache")


def test_order_and_limit_are_passed_through(configured):
    fake = FakeRest({"ai_trade_events": []})
    supabase_rest.select("ai_trade_events", order="at.desc", limit=50, client=fake.client())
    params = fake.requests[0]["params"]
    assert params["order"] == "at.desc" and params["limit"] == "50"


def test_an_unfiltered_delete_is_refused():
    """PostgREST refuses it too; keeping the guard here means the refusal
    is explicit rather than a puzzling 4xx."""
    with pytest.raises(supabase_rest.SupabaseError):
        supabase_rest.delete("analysis_cache", {})


def test_an_error_response_is_raised_not_swallowed(configured):
    def failing(request):
        return httpx.Response(401, text="Invalid API key")
    client = httpx.Client(transport=httpx.MockTransport(failing))
    with pytest.raises(supabase_rest.SupabaseError) as exc:
        supabase_rest.select("analysis_cache", client=client)
    assert "401" in str(exc.value)


def test_not_configured_unless_both_url_and_key_are_set(monkeypatch):
    monkeypatch.delenv(supabase_rest.URL_ENV, raising=False)
    monkeypatch.delenv(supabase_rest.KEY_ENV, raising=False)
    assert supabase_rest.configured() is False
    monkeypatch.setenv(supabase_rest.URL_ENV, "https://x.supabase.co")
    assert supabase_rest.configured() is False
    monkeypatch.setenv(supabase_rest.KEY_ENV, "k")
    assert supabase_rest.configured() is True


def _route_through(monkeypatch, fake):
    """Send every REST call through the fake transport.

    The original function is captured FIRST: patching the name and then
    calling it through the module would call the patch, not the real
    implementation."""
    original = supabase_rest._request

    def routed(method, path, params=None, json_body=None, extra_headers=None,
               client=None, timeout=supabase_rest.DEFAULT_TIMEOUT):
        return original(method, path, params, json_body, extra_headers, fake.client(), timeout)

    monkeypatch.setattr(supabase_rest, "_request", routed)


# ---- the two stores route to REST only when it is configured -----------

def test_an_explicit_database_url_always_wins(tmp_path, configured):
    """A test (or a caller naming its own database) must not be redirected
    to the deployment's storage just because the environment has it."""
    url = f"sqlite:///{tmp_path / 'explicit.db'}"
    analysis_store._factory = None
    analysis_store.save("bybit", "X", "5m", 1, 1, {"ok": True}, database_url=url)
    assert analysis_store.load("bybit", "X", "5m", database_url=url) is not None
    assert analysis_store._use_rest(url) is False
    assert analysis_store._use_rest(None) is True


def test_a_saved_analysis_round_trips_through_rest(monkeypatch, configured):
    fake = FakeRest({"analysis_cache": []})
    _route_through(monkeypatch, fake)

    analysis_store.save("bybit", "INJUSDT", "4h", 1700, 500,
                        {"accepted": [{"structure": "IMPULSE"}]}, model="m")
    # the old row is cleared first - one analysis per series, never two
    assert [r["method"] for r in fake.requests[:2]] == ["DELETE", "POST"]

    cached = analysis_store.load("bybit", "INJUSDT", "4h")
    assert cached is not None
    assert cached.payload["accepted"][0]["structure"] == "IMPULSE"
    assert cached.last_candle_time == 1700


def test_a_journalled_fill_round_trips_through_rest(monkeypatch, configured):
    fake = FakeRest({"ai_trade_events": []})
    _route_through(monkeypatch, fake)

    trade_journal.record(TradeEvent(
        source="bybit", symbol="INJUSDT", timeframe="5m", position_id="p1",
        event="TP_HIT", side="LONG", price=6.19, quantity=1.0, realized_pnl=4.0,
        position_realized_pnl=4.0, label="TP1", wave_label="3", at=1_700_000_000_000))

    rows = trade_journal.events_for("bybit", "INJUSDT", "5m")
    assert rows and rows[0]["label"] == "TP1"
    summary = trade_journal.summary_for("bybit", "INJUSDT", "5m")
    assert summary["take_profits_hit"] == 1 and summary["realized_pnl"] == 4.0


def test_a_rest_failure_never_breaks_the_trading_path(monkeypatch, configured):
    def explode(*a, **k):
        raise supabase_rest.SupabaseError("service unavailable")
    monkeypatch.setattr(supabase_rest, "insert", explode)
    monkeypatch.setattr(supabase_rest, "select", explode)
    trade_journal.record(TradeEvent(source="b", symbol="S", timeframe="5m", position_id="p",
                                    event="ENTRY", side="LONG", price=1.0, quantity=1.0))
    assert trade_journal.events_for("b", "S") == []
