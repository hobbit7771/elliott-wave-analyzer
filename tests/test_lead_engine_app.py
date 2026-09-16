"""The ChatGPT app layer: widgets over the same read-only MCP server.

A connector returns JSON and the model reads it out loud; an app returns a
COMPONENT. These tests pin the three things that make the difference and
the two that make it safe.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from lead_engine_mcp import widgets
from lead_engine_mcp.server import handle
from lead_engine_mcp.tools import TOOLS, tool_list


# ---- discovery ---------------------------------------------------------

def test_initialize_advertises_resources_or_no_host_will_ask_for_them():
    """A host only fetches widgets if the server said it has any. Without
    this capability the `_meta` on the tools is dead weight."""
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    caps = reply["result"]["capabilities"]
    assert "resources" in caps
    assert caps["tools"]["listChanged"] is False


def test_every_widget_is_listed_and_readable():
    listed = handle({"jsonrpc": "2.0", "id": 2,
                     "method": "resources/list", "params": {}})["result"]["resources"]
    assert len(listed) == len(widgets.WIDGETS)
    for entry in listed:
        assert entry["mimeType"] == widgets.MIME
        read = handle({"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                       "params": {"uri": entry["uri"]}})
        content = read["result"]["contents"][0]
        assert content["mimeType"] == widgets.MIME
        assert content["text"].startswith("<!doctype html>")
        assert "<script>" in content["text"]


def test_an_unknown_resource_is_an_error_not_a_crash():
    reply = handle({"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                    "params": {"uri": "ui://widget/../../etc/passwd"}})
    assert reply["error"]["code"] == -32602


def test_templates_list_answers_empty_rather_than_erroring():
    """A fixed set of widgets has no templates. Answering -32601 makes
    every host log an error on every connection for nothing."""
    reply = handle({"jsonrpc": "2.0", "id": 5,
                    "method": "resources/templates/list", "params": {}})
    assert reply["result"]["resourceTemplates"] == []


def test_each_widget_tool_points_at_a_widget_that_exists():
    """A dangling `openai/outputTemplate` renders nothing and says
    nothing, so the link is checked rather than trusted."""
    uris = {entry["uri"] for entry in widgets.resource_list()}
    listed = {tool["name"]: tool["_meta"] for tool in tool_list()}
    for name in widgets.TOOL_WIDGETS:
        assert name in TOOLS, f"{name} is not a real tool"
        template = listed[name]["openai/outputTemplate"]
        assert template in uris
        assert listed[name]["openai/toolInvocation/invoking"]
        assert listed[name]["openai/toolInvocation/invoked"]


def test_tools_without_a_widget_carry_no_widget_meta():
    listed = {tool["name"]: tool["_meta"] for tool in tool_list()}
    for name in TOOLS:
        if name not in widgets.TOOL_WIDGETS:
            assert "openai/outputTemplate" not in listed[name], name
    # And every tool still carries the schema version it always did.
    assert all("schema_version" in meta for meta in listed.values())


# ---- the split that keeps it cheap -------------------------------------

def _snapshot_payload():
    return {
        "symbol": "INJUSDT", "price": 12.34,
        "signal": {"state": "PRE_BREAK_LONG", "direction": "long",
                   "break_probability": 68.0, "level": 12.5},
        "pressure": {"long_pressure": 71.0, "short_pressure": 12.0,
                     "confidence": 0.8, "conflict_level": "CONFLICT_LOW"},
        "layers": {"flow": {"score": 0.4, "confidence": 1.0},
                   "book": {"score": -0.2, "confidence": 0.9}},
        "health": {"status": "OK", "signals_valid": True, "book_age_ms": 94},
        "orderbook": {"bids": [[1, 2]] * 50, "asks": [[1, 2]] * 50},
    }


def test_the_journal_goes_to_the_widget_and_not_into_the_model_context():
    """`structuredContent` is injected into the model's context on EVERY
    call. A forty-row journal in there is forty rows of tokens per call,
    for a table the model was never asked to read. It belongs in `_meta`,
    which only the widget sees."""
    journal = [{"id": f"t{i}", "net_pnl": 0.1, "direction": "long",
                "status": "CLOSED", "signal_at_ms": 1_700_000_000_000 + i}
               for i in range(40)]
    payload = {"summary": {"symbol": "INJUSDT", "net_pnl": 4.0,
                           "closed_trades": 40, "wins": 25, "losses": 15},
               "journal": journal}
    split = widgets.split_payload("get_virtual_trades", payload)

    assert split["structured"]["closed_trades"] == 40      # the summary
    assert "journal" not in split["structured"]
    assert len(split["meta"]["journal"]) == 40             # the rows


def test_the_snapshot_split_keeps_the_signal_and_drops_the_ladder():
    split = widgets.split_payload("get_market_snapshot", _snapshot_payload())
    structured = split["structured"]
    assert structured["signal"]["state"] == "PRE_BREAK_LONG"
    assert structured["pressure"]["long_pressure"] == 71.0
    # A hundred price levels are not something a model should be made to
    # read to answer "what is the book doing".
    assert "orderbook" not in structured
    assert {row["name"] for row in split["meta"]["layers"]} == {"flow", "book"}


def test_an_error_payload_is_not_dressed_up_as_structured_content():
    split = widgets.split_payload("get_virtual_trades", {"error": "nope"})
    assert split["meta"] == {}
    assert split["structured"] == {"error": "nope"}


def test_a_failed_call_carries_no_structured_content(monkeypatch):
    reply = handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "get_virtual_trades", "arguments": {}}})
    result = reply["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result


def test_a_successful_call_carries_all_three_channels(monkeypatch):
    """text content for a transcript, structuredContent for the model,
    _meta for the widget."""
    from lead_engine_mcp import tools as tools_module

    monkeypatch.setitem(
        tools_module.TOOLS["get_market_snapshot"], "call",
        lambda c, a: _snapshot_payload())
    reply = handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "get_market_snapshot",
                               "arguments": {"symbol": "INJUSDT"}}})
    result = reply["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["symbol"] == "INJUSDT"
    assert result["structuredContent"]["symbol"] == "INJUSDT"
    assert result["_meta"]["layers"]
    assert result["_meta"]["schema_version"]


# ---- what makes it safe ------------------------------------------------

def test_no_widget_can_reach_the_network():
    """A widget that fetched would need the API token inside the browser,
    which publishes a credential to every viewer - and it could show the
    model something the model never saw. They render from `toolOutput`
    and nothing else."""
    for name, spec in widgets.WIDGETS.items():
        html = spec["html"].lower()
        for forbidden in ("fetch(", "xmlhttprequest", "websocket", "import(",
                          "https://", "http://", "<link", "src="):
            assert forbidden not in html, f"{name} contains {forbidden!r}"


def test_the_app_layer_still_imports_no_engine():
    """The same isolation the rest of this package has: the adapter is not
    the engine, and a widget is presentation, not a second source of
    truth."""
    source = Path("lead_engine_mcp/widgets.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("t3_engine"), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("t3_engine"), node.module


def test_no_widget_tool_can_write_anything():
    """Widgets are attached only to read tools, and there are no others -
    but a widget attached to a mutation would be the first thing to
    notice, so it is asserted."""
    listed = {tool["name"]: tool for tool in tool_list()}
    for name in widgets.TOOL_WIDGETS:
        assert listed[name]["annotations"]["readOnlyHint"] is True
        assert listed[name]["annotations"]["destructiveHint"] is False
        assert listed[name]["_meta"]["openai/widgetAccessible"] is False


def test_every_widget_renders_without_a_payload():
    """The host can mount an iframe before the data lands. A widget that
    throws on an empty `toolOutput` shows a blank card at exactly the
    moment someone is watching it appear."""
    for name, spec in widgets.WIDGETS.items():
        html = spec["html"]
        # `payload()` defaults both halves, and every draw() reads through
        # `|| {}` guards rather than indexing straight in.
        assert "api.toolOutput || {}" in html, name
        assert "p.data || {}" in html, name


# ---- protocol negotiation ----------------------------------------------

@pytest.mark.parametrize("wanted", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_the_client_version_is_echoed_when_it_is_one_we_speak(wanted):
    """The spec has the client state its version and the server answer
    with the one it will use. Answering with a fixed string regardless
    tells a client on a newer revision that the server only does an older
    one, and a strict host can refuse the connection over a difference
    that does not exist - every method here behaves identically on all
    three."""
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": wanted}})
    assert reply["result"]["protocolVersion"] == wanted


@pytest.mark.parametrize("wanted", ["1999-01-01", "", None, 7])
def test_an_unknown_version_gets_our_newest_rather_than_an_error(wanted):
    """The client decides whether it can live with the answer; refusing
    outright would break a host that is merely newer than this server."""
    from lead_engine_mcp.server import PROTOCOL_VERSION

    params = {} if wanted is None else {"protocolVersion": wanted}
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": params})
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_the_journal_puts_the_money_where_a_phone_can_see_it():
    """The journal table scrolls sideways in a narrow iframe. Net P&L in
    the seventh column sat off-screen at 420px, which is the one number
    someone opens a P&L journal to read."""
    html = widgets.WIDGETS["virtual-trades"]["html"]
    headers = html[html.index("<th>time</th>"):html.index("body + '</table>")]
    order = [part for part in ("time", "side", "net", "why", "signal",
                               "entry", "exit") if f">{part}</th>" in headers]
    assert order[:4] == ["time", "side", "net", "why"]
