"""ChatGPT Apps SDK widgets: the engine as an app, not just a connector.

A connector returns JSON and the model reads it out loud. An app returns a
COMPONENT: the pressure gauges, the freshness clocks and the virtual P&L
journal render inside the conversation, in the shape they have on the
dashboard, and the model talks about what the person is looking at.

The mechanics, and the two that are easy to get wrong:

  RESOURCES     each widget is an MCP resource at `ui://widget/<name>.html`
                with mimeType `text/html+skybridge`. A tool opts into one
                by naming it in `_meta["openai/outputTemplate"]`.

  TWO CHANNELS, DIFFERENT AUDIENCES. A tool result carries
  `structuredContent` AND `_meta`, and they are not interchangeable:

    structuredContent  goes to the WIDGET *and into the model's context*.
                       Everything here costs tokens on every call, so it
                       holds the summary a model needs to talk about the
                       result - not the rows.
    _meta              goes to the WIDGET ONLY. The forty-row journal, the
                       per-level ladders and anything else the component
                       draws but the model should not be made to read
                       lives here.

  Putting a journal in `structuredContent` is the commonest way an app
  becomes slow and expensive without looking wrong.

  NO NETWORK IN A WIDGET. These components render from `toolOutput` and
  nothing else: no fetch, no CDN, no external stylesheet. Three reasons,
  all of them decisive. ChatGPT's iframe CSP blocks most of it anyway; a
  widget that fetched would need the API token inside the browser, which
  would publish a credential to every viewer; and a component that can
  re-query is a component that can show something the model never saw.

Everything here is presentation. This module reaches no engine, holds no
credential, and - like the rest of `lead_engine_mcp` - imports no
`t3_engine`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

MIME = "text/html+skybridge"

# Shared by every widget. Inlined rather than linked for the reasons in
# the module docstring, and written against the system font stack because
# a webfont is one more thing the iframe would have to fetch.
_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0b0e14; color: #d7dbe3;
       font: 13px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { padding: 12px; }
.card { border: 1px solid #232733; border-radius: 10px; background: #11151d;
        padding: 12px 14px; margin: 0 0 10px; }
h3 { margin: 0 0 8px; font-size: 11px; letter-spacing: .08em;
     text-transform: uppercase; color: #9aa3b2; font-weight: 600; }
.row { display: flex; justify-content: space-between; gap: 12px;
       padding: 4px 0; border-bottom: 1px solid #171c25; }
.row:last-child { border-bottom: none; }
.k { color: #8a93a5; }
.v { text-align: right; font-variant-numeric: tabular-nums; }
.up { color: #34d399; } .down { color: #f87171; } .dim { color: #6b7488; }
.note { color: #7c869a; font-size: 11px; margin: 8px 0 0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px;
         border: 1px solid #3a4152; font-size: 11px; letter-spacing: .04em; }
.badge.long  { color: #34d399; border-color: #1c5e50; background: #0f2620; }
.badge.short { color: #f87171; border-color: #6b2222; background: #260f0f; }
.badge.aplus { color: #fde68a; border-color: #6b551b; background: #241d0c; }
.badge.rev   { color: #a78bfa; border-color: #4c3a86; background: #191430; }
.badge.fail  { color: #f87171; border-color: #6b2222; background: #1c0f0f; }
.meter { margin: 8px 0; }
.meter .lbl { display: flex; justify-content: space-between; font-size: 11px;
              color: #8a93a5; margin-bottom: 3px; }
.meter .track { height: 7px; background: #1a1f29; border-radius: 4px; overflow: hidden; }
.meter .fill { display: block; height: 100%; width: 0; border-radius: 4px; }
.meter.long .fill  { background: linear-gradient(90deg, #12614e, #34d399); }
.meter.short .fill { background: linear-gradient(90deg, #6b2222, #f87171); }
table { width: 100%; border-collapse: collapse; font-size: 11.5px;
        font-variant-numeric: tabular-nums; }
th { text-align: left; color: #6b7488; font-weight: 500; font-size: 10px;
     text-transform: uppercase; letter-spacing: .05em; padding: 4px 6px;
     border-bottom: 1px solid #232733; }
td { padding: 4px 6px; border-bottom: 1px solid #171c25; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
.scroll { overflow-x: auto; }
.empty { color: #7c869a; font-size: 12px; padding: 10px 0; }
@media (prefers-color-scheme: light) {
  body { background: #fbfbfa; color: #21242c; }
  .card { background: #fff; border-color: #e3e5ea; }
  .row { border-bottom-color: #eef0f3; }
  .k, .note, th { color: #6b7280; }
  td { border-bottom-color: #f0f1f4; }
}
"""

# Read the tool payload the same way in every widget. `toolOutput` is the
# structuredContent; `_meta` arrives beside it and carries the rows.
_BOOT = """
function payload() {
  var api = window.openai || {};
  var out = api.toolOutput || {};
  var meta = (api.toolResponseMetadata) || out._meta || {};
  return { data: out, meta: meta };
}
function esc(t) {
  return String(t === undefined || t === null ? '' : t)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function num(v, d) {
  if (v === undefined || v === null || v === '') return '\\u2014';
  var n = Number(v); if (!isFinite(n)) return '\\u2014';
  return n.toFixed(d === undefined ? 2 : d);
}
function signed(v, d) {
  if (v === undefined || v === null) return '\\u2014';
  var n = Number(v); if (!isFinite(n)) return '\\u2014';
  return (n > 0 ? '+' : '') + n.toFixed(d === undefined ? 2 : d);
}
function tone(v) {
  var n = Number(v);
  return (!isFinite(n) || n === 0) ? 'dim' : (n > 0 ? 'up' : 'down');
}
function row(k, v, cls) {
  return '<div class="row"><span class="k">' + esc(k) + '</span>' +
         '<span class="v ' + (cls || '') + '">' + v + '</span></div>';
}
function render() {
  try { draw(payload()); }
  catch (e) {
    document.getElementById('root').innerHTML =
      '<div class="card"><div class="empty">Widget error: ' + esc(e.message) + '</div></div>';
  }
}
// Rendered once on load and again whenever the host swaps the payload in
// (a follow-up call reuses the same iframe).
document.addEventListener('DOMContentLoaded', render);
window.addEventListener('openai:set_globals', render);
"""


def _page(title: str, draw_js: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title><style>{_CSS}</style></head>"
        "<body><div id=\"root\" class=\"wrap\"></div>"
        f"<script>{_BOOT}\n{draw_js}</script></body></html>"
    )


_SNAPSHOT_JS = """
function stateClass(s) {
  if (s === 'A_PLUS' || s === 'HIGH_PROBABILITY') return 'aplus';
  if (s === 'PRE_BREAK_LONG') return 'long';
  if (s === 'PRE_BREAK_SHORT') return 'short';
  if (s === 'REVERSAL_CANDIDATE') return 'rev';
  if (s === 'DATA_FAILURE' || s === 'INVALIDATED') return 'fail';
  return '';
}
function meter(label, value, cls) {
  var pct = Math.max(0, Math.min(100, Number(value) || 0));
  return '<div class="meter ' + cls + '"><div class="lbl"><span>' + esc(label) +
    '</span><span>' + num(value, 1) + '</span></div>' +
    '<div class="track"><span class="fill" style="width:' + pct + '%"></span></div></div>';
}
function draw(p) {
  var d = p.data || {}, m = p.meta || {};
  var sig = d.signal || {}, pr = d.pressure || {};
  var html = '';

  html += '<div class="card"><h3>' + esc(d.symbol || '') + '</h3>' +
    '<div style="margin:2px 0 10px"><span class="badge ' + stateClass(sig.state) + '">' +
      esc(sig.state || 'IDLE') + '</span>' +
      ' <span class="badge dim">MODEL SCORE</span></div>' +
    row('\\u0426\\u0435\\u043d\\u0430', num(d.price, 4)) +
    row('Direction', esc(sig.direction || '\\u2014')) +
    row('Level', num(sig.level, 4)) +
    row('Break score', num(sig.break_probability, 1)) +
    meter('LONG_PRESSURE', pr.long_pressure, 'long') +
    meter('SHORT_PRESSURE', pr.short_pressure, 'short') +
    row('Confidence', num(pr.confidence, 2)) +
    row('Conflict', esc(pr.conflict_level || '\\u2014')) +
    (sig.reason ? '<div class="note">' + esc(sig.reason) + '</div>' : '') +
    '</div>';

  var layers = m.layers || [];
  if (layers.length) {
    var rows = layers.map(function (l) {
      return '<tr><td>' + esc(l.name) + '</td>' +
        '<td class="' + tone(l.score) + '" style="text-align:right">' + signed(l.score, 2) + '</td>' +
        '<td style="text-align:right;color:#6b7488">' + num(l.confidence, 2) + '</td></tr>';
    }).join('');
    html += '<div class="card"><h3>Layers</h3><table>' +
      '<tr><th>layer</th><th style="text-align:right">score</th>' +
      '<th style="text-align:right">conf</th></tr>' + rows + '</table></div>';
  }

  var h = d.health || {};
  html += '<div class="card"><h3>Data freshness</h3>' +
    row('Status', esc(h.status || '\\u2014'),
        h.status === 'OK' ? 'up' : 'down') +
    row('Signals', h.signals_valid ? 'enabled' : 'OFF',
        h.signals_valid ? 'up' : 'down') +
    row('Book age', num(h.book_age_ms, 0) + ' ms') +
    row('Trade age', num(h.trade_age_ms, 0) + ' ms') +
    '<div class="note">Book age is not latency: it is how old the exchange ' +
    'says this book is. Treat pressure and break scores as MODEL SCORES ' +
    'unless calibration says PROBABILITY.</div></div>';

  document.getElementById('root').innerHTML = html;
}
"""

_TRADES_JS = """
var STATUS = { PENDING: 'waiting for book', OPEN: 'open', CLOSED: 'closed',
               ABANDONED: 'not filled' };
function clock(ms) {
  if (!ms) return '\\u2014';
  var d = new Date(Number(ms));
  if (isNaN(d.getTime())) return '\\u2014';
  return ('0' + d.getUTCHours()).slice(-2) + ':' + ('0' + d.getUTCMinutes()).slice(-2) +
         ':' + ('0' + d.getUTCSeconds()).slice(-2);
}
function draw(p) {
  var s = p.data || {}, m = p.meta || {};
  var decided = (s.wins || 0) + (s.losses || 0);
  var html = '<div class="card"><h3>' + esc(s.symbol || '') +
    ' \\u2014 virtual ledger <span class="badge dim">PAPER</span></h3>' +
    row('Closed trades', num(s.closed_trades, 0)) +
    row('Open / pending', num(s.open_positions, 0) + ' / ' + num(s.pending_intents, 0)) +
    row('Win rate', decided ? num(100 * (s.wins || 0) / decided, 1) + '% (' +
        (s.wins || 0) + '/' + decided + ')' : '\\u2014') +
    row('Gross P&L', signed(s.gross_pnl, 4), tone(s.gross_pnl)) +
    row('Fees', s.fees_paid ? '-' + num(s.fees_paid, 4) : '0.0000') +
    row('Net P&L', '<b>' + signed(s.net_pnl, 4) + '</b>', tone(s.net_pnl)) +
    row('Open P&L', signed(s.open_pnl, 4), tone(s.open_pnl)) +
    row('Not filled (stream gap)', num(s.abandoned_on_gap, 0)) +
    row('Uncertain exits', num(s.gap_uncertain_exits, 0)) +
    '</div>';

  var rows = (m.journal || []);
  if (rows.length) {
    var body = rows.map(function (r) {
      var why = r.status === 'CLOSED' ? (r.exit_reason || '')
                                      : (STATUS[r.status] || r.status);
      // Net P&L comes third, not last. The table scrolls sideways on a
      // phone, and in a P&L journal the money is the point: in the
      // seventh column it sat off-screen, so the one number the reader
      // opened this for was the one they had to scroll to find.
      return '<tr' + (r.status === 'ABANDONED' ? ' style="opacity:.55"' : '') + '>' +
        '<td class="dim">' + esc(clock(r.signal_at_ms)) + '</td>' +
        '<td class="' + (r.direction === 'long' ? 'up' : 'down') + '">' +
          esc(String(r.direction || '').toUpperCase()) + '</td>' +
        '<td style="text-align:right" class="' +
          (r.status === 'CLOSED' ? tone(r.net_pnl) : 'dim') + '">' +
          (r.status === 'CLOSED' ? signed(r.net_pnl, 4) : '\\u2014') + '</td>' +
        '<td>' + esc(why) + (r.gap_uncertain ? ' ~' : '') + '</td>' +
        '<td class="dim">' + esc(r.signal_state || '') + '</td>' +
        '<td style="text-align:right">' + num(r.entry_price, 5) + '</td>' +
        '<td style="text-align:right">' + num(r.exit_price, 5) + '</td></tr>';
    }).join('');
    html += '<div class="card"><h3>Journal</h3><div class="scroll"><table>' +
      '<tr><th>time</th><th>side</th><th style="text-align:right">net</th>' +
      '<th>why</th><th>signal</th>' +
      '<th style="text-align:right">entry</th>' +
      '<th style="text-align:right">exit</th></tr>' +
      body + '</table></div></div>';
  } else {
    html += '<div class="card"><div class="empty">No actionable signal has ' +
      'traded yet. WATCH and IDLE are not entries.</div></div>';
  }

  var c = s.config || {};
  html += '<div class="note">Entry is taken at the first fresh book AFTER ' +
    'the signal, crossing the spread, with ' + num((c.taker_fee || 0) * 100, 3) +
    '% taker fees both ways and ' + num(c.slippage_bps, 1) + ' bps slippage ' +
    'deducted. If the stream breaks for longer than ' + num(c.max_fill_gap_ms, 0) +
    ' ms the trade is recorded as NOT FILLED rather than filled at a price ' +
    'nobody observed. These positions exist nowhere but this ledger.</div>';

  document.getElementById('root').innerHTML = html;
}
"""

_HEALTH_JS = """
function draw(p) {
  var d = p.data || {}, m = p.meta || {};
  var rows = m.symbols || [];
  var html = '<div class="card"><h3>Feed health</h3>' +
    row('Engine', d.enabled ? 'running' : 'off', d.enabled ? 'up' : 'down') +
    row('Symbols', num(rows.length, 0)) +
    row('Signals valid', num(d.signals_valid_count, 0) + ' / ' + num(rows.length, 0)) +
    '</div>';
  if (rows.length) {
    var body = rows.map(function (r) {
      return '<tr><td>' + esc(r.symbol) + '</td>' +
        '<td class="' + (r.signals_valid ? 'up' : 'down') + '">' +
          esc(r.status || '') + '</td>' +
        '<td style="text-align:right">' + num(r.book_age_ms, 0) + '</td>' +
        '<td style="text-align:right">' + num(r.trade_age_ms, 0) + '</td>' +
        '<td class="dim">' + esc(r.reason || '') + '</td></tr>';
    }).join('');
    html += '<div class="card"><div class="scroll"><table>' +
      '<tr><th>symbol</th><th>status</th><th style="text-align:right">book ms</th>' +
      '<th style="text-align:right">trade ms</th><th>why</th></tr>' +
      body + '</table></div>' +
      '<div class="note">Book age is how old the exchange says the book is, ' +
      'measured from arrival - not round-trip latency. A stale book disables ' +
      'signals even while the socket is connected.</div></div>';
  }
  document.getElementById('root').innerHTML = html;
}
"""

# name -> (title, page html). The URI is derived, so it cannot drift.
WIDGETS: Dict[str, Dict[str, str]] = {
    "market-snapshot": {
        "title": "Market snapshot",
        "html": _page("Market snapshot", _SNAPSHOT_JS),
    },
    "virtual-trades": {
        "title": "Virtual trade ledger",
        "html": _page("Virtual trade ledger", _TRADES_JS),
    },
    "feed-health": {
        "title": "Feed health",
        "html": _page("Feed health", _HEALTH_JS),
    },
}

# Which tool renders into which widget.
TOOL_WIDGETS: Dict[str, str] = {
    "get_market_snapshot": "market-snapshot",
    "get_virtual_trades": "virtual-trades",
    "get_health": "feed-health",
}

# What the host shows while the tool runs, and after.
INVOCATION_TEXT: Dict[str, Dict[str, str]] = {
    "get_market_snapshot": {"invoking": "Reading the order book",
                            "invoked": "Live microstructure"},
    "get_virtual_trades": {"invoking": "Opening the paper ledger",
                           "invoked": "Virtual P&L"},
    "get_health": {"invoking": "Checking feed freshness",
                   "invoked": "Feed health"},
}


def widget_uri(name: str) -> str:
    return f"ui://widget/{name}.html"


def resource_list() -> List[Dict[str, Any]]:
    """The MCP `resources/list` payload."""
    return [
        {
            "uri": widget_uri(name),
            "name": spec["title"],
            "description": f"{spec['title']} component, rendered from the "
                           f"tool result. Read-only and offline: it draws "
                           f"what the tool returned and fetches nothing.",
            "mimeType": MIME,
        }
        for name, spec in sorted(WIDGETS.items())
    ]


def resource_read(uri: str) -> Optional[Dict[str, Any]]:
    """One widget's HTML, or None when the URI is not one of ours."""
    for name, spec in WIDGETS.items():
        if uri == widget_uri(name):
            return {
                "uri": uri,
                "mimeType": MIME,
                "text": spec["html"],
                "_meta": {
                    # No CSP relaxation is requested because no widget
                    # needs one: none of them fetch anything.
                    "openai/widgetPrefersBorder": True,
                    "openai/widgetDescription": spec["title"],
                },
            }
    return None


def tool_meta(name: str) -> Dict[str, Any]:
    """The `_meta` a tool descriptor needs to opt into its widget."""
    widget = TOOL_WIDGETS.get(name)
    if widget is None:
        return {}
    text = INVOCATION_TEXT.get(name, {})
    meta: Dict[str, Any] = {"openai/outputTemplate": widget_uri(widget)}
    if text.get("invoking"):
        meta["openai/toolInvocation/invoking"] = text["invoking"]
    if text.get("invoked"):
        meta["openai/toolInvocation/invoked"] = text["invoked"]
    # None of these tools can change anything, so none of them should
    # prompt for confirmation.
    meta["openai/widgetAccessible"] = False
    return meta


def _layer_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        return []
    rows = []
    for name, layer in layers.items():
        if isinstance(layer, dict):
            rows.append({"name": name,
                         "score": layer.get("score"),
                         "confidence": layer.get("confidence")})
    return rows


def split_payload(name: str, payload: Any) -> Dict[str, Any]:
    """Divide one tool result into what the MODEL sees and what only the
    WIDGET sees.

    The split is the whole point (see the module docstring):
    `structuredContent` is injected into the model's context on every
    call, so it carries the summary and never the rows. A forty-row
    journal in there is forty rows of tokens per call, every call, for a
    table the model is not being asked to read."""
    if not isinstance(payload, dict) or "error" in payload:
        return {"structured": payload if isinstance(payload, dict) else {},
                "meta": {}}

    if name == "get_market_snapshot":
        keep = ("symbol", "price", "signal", "pressure", "health",
                "server_time", "data_age_ms", "quality", "signals_valid")
        structured = {k: payload[k] for k in keep if k in payload}
        return {"structured": structured, "meta": {"layers": _layer_rows(payload)}}

    if name == "get_virtual_trades":
        summary = payload.get("summary") or {}
        return {"structured": summary,
                "meta": {"journal": payload.get("journal") or []}}

    if name == "get_health":
        rows = payload.get("symbols")
        rows = rows if isinstance(rows, list) else []
        slim = [{"symbol": r.get("symbol"), "status": r.get("status"),
                 "book_age_ms": r.get("book_age_ms"),
                 "trade_age_ms": r.get("trade_age_ms"),
                 "signals_valid": r.get("signals_valid"),
                 "reason": (r.get("reasons") or [""])[0]
                           if isinstance(r.get("reasons"), list) else r.get("reason")}
                for r in rows if isinstance(r, dict)]
        return {
            "structured": {
                "enabled": payload.get("enabled", True),
                "symbol_count": len(slim),
                "signals_valid_count": sum(1 for r in slim if r.get("signals_valid")),
                "server_time": payload.get("server_time"),
            },
            "meta": {"symbols": slim},
        }

    return {"structured": payload, "meta": {}}


def widget_resources_json() -> str:
    """For a test or a doc that wants the surface as data."""
    return json.dumps(resource_list(), indent=2)
