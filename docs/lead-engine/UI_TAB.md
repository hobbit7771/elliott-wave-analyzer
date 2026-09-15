# The tab

**Market Lead Engine**, a sibling of the existing tabs, rendered entirely
by its own files:

* `t3_engine/dashboard/static/lead_engine.js` — all of the rendering
* `t3_engine/dashboard/static/lead_engine.css` — all of the styling,
  scoped under `#tab-lead` and `.le-`, so loading it cannot change how any
  other tab looks

`index.html` gains exactly four things: a tab button, an **empty** panel
div, a stylesheet link and a script tag — plus one entry in the tab table
and one `show()`/`hide()` pair in the switcher. No rendering code for this
tab lives in the shared script, and a test asserts it.

## State

One global, `leadEngineStore`. It reads no state belonging to any other
tab and no other tab reads it.

## Polling, and only while visible

The tab polls `GET /api/lead-engine/state/{symbol}` every second and
`/status` every four. **Both intervals are cleared the moment the tab is
hidden**, which is the brief's requirement that an unopened tab cost
nothing. Measured in a browser: zero `/api/lead-engine/*` requests before
the tab is opened, and zero further requests after leaving it.

Polling rather than a browser WebSocket: the exchange socket is on the
server, and a second one from the browser would be a second subscription
to the same data with no way to keep the two in step.

## Layout

**Header** — price, symbol, feed status, WebSocket up/down, latency, order
book age, processing time, signals on/off.

**Blocks** — Active signal · Pressure · Order book · Microprice · Trade
flow · Liquidations · Open interest · BTC lead · Structure · Pre-break
short · Pre-break long · Fleet.

Pressure is drawn as **two separate bars**, not one two-sided meter, for
the reason in PRESSURE_SCORE.md: long and short are independent and both
can be high at once.

Pre-break shows every one of its ten features as its own bar, so a
probability can be taken apart on screen without opening the API.

**Fleet** is every subscribed symbol at once — state, both pressures and
feed health — because the engine watches seven instruments and the
interesting one is not always the one being displayed.

## When the engine is off

The tab renders a plain panel saying so, naming the flag, and makes no
repeated requests. It never shows an error: "switched off" and "broken"
are different things and the tab says which.
