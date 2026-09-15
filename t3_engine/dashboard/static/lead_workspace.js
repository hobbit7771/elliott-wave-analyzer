/* Market Workspace: one symbol, a real chart, and the Lead Engine panel.
 *
 * The three rules that keep it fast:
 *
 *  HISTORY ONCE   candles are fetched once per (symbol, timeframe) and
 *                 `setData` is called once. After that only `update()`
 *                 runs, on the one bar that is forming. Re-sending 500
 *                 candles every tick is the mistake this avoids.
 *  INCREMENTAL    a tick mutates the live bar; a bar closing appends a
 *                 new one. EMAs extend by one point rather than being
 *                 recomputed over the whole series.
 *  THROTTLED      the panel repaints through LeadPanel at ~300ms while
 *                 state is fetched at 500ms — calculation rate and
 *                 display rate are separate, as the brief requires.
 *
 * Markers use the signal's OWN timestamp. A marker is never moved to a
 * better pivot after the fact: that would make every backtest built on
 * this chart a lie, which is why `changed_at` is taken from the engine
 * and never recomputed here.
 */
(function (global) {
  'use strict';

  var TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'];
  var TF_SECONDS = { '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
                     '1h': 3600, '4h': 14400, '1d': 86400 };
  var FETCH_MS = 500;
  var REFRESH_MS = 300;
  // How often the forming bar is re-read from Bybit. Every bar boundary
  // triggers one as well, so this is the in-between cadence rather than
  // the only one.
  var LIVE_BAR_MS = 4000;
  // Beyond this the live price is not a live price and must not be drawn
  // onto the current bar. Matches the engine's own DEGRADED threshold.
  var STALE_DRAW_MS = 1000;
  var EMA_COLORS = { 9: '#38bdf8', 18: '#34d399', 50: '#fbbf24', 200: '#f472b6' };
  var PREFS_KEY = 'lead_ws_prefs';

  var ws = {
    symbol: null, timeframe: '5m', chart: null, series: null, volume: null,
    emaSeries: {}, emaAnchor: {}, candles: [], closes: [], panel: null, fib: null,
    frame: null, timer: null, liveTimer: null, markers: [], uiLatencyMs: null,
    // Bumped on every symbol or timeframe change. An in-flight response
    // that comes back carrying an older token is DISCARDED: switching
    // 5m -> 1m -> 15m quickly otherwise lets the slowest response land
    // last and paint the wrong series over the right one.
    generation: 0,
    // server clock minus browser clock. Bar bucketing uses the corrected
    // time, because a browser clock that is thirty seconds out puts every
    // tick in the wrong bar and opens each new bar at the wrong moment.
    clockSkewMs: 0,
    liveStale: false,
    prefs: { ema: { 9: false, 18: false, 50: true, 200: true },
             volume: true, signals: true },
    fetches: 0, updates: 0, setDataCalls: 0
  };
  global.leadWorkspace = ws;

  function el(id) { return document.getElementById(id); }

  /* The server's clock, not this browser's. Every frame carries the time
     the server built it; the difference is tracked and applied so bar
     boundaries land where the exchange says they do. */
  function exchangeNow() { return Date.now() + ws.clockSkewMs; }

  function noteServerClock(frame) {
    var serverMs = null;
    if (frame && typeof frame.generated_at === 'number') {
      serverMs = frame.generated_at * 1000;
    } else if (frame && typeof frame.server_time === 'number') {
      serverMs = frame.server_time;
    }
    if (serverMs === null) return;
    // Half the round trip is the honest correction; the poll interval is
    // 500ms so this is accurate to a few tens of milliseconds, which is
    // far inside a one-minute bar.
    var skew = serverMs + (ws.uiLatencyMs || 0) / 2 - Date.now();
    // Smoothed, so one slow response does not jolt the bucketing.
    ws.clockSkewMs = ws.clockSkewMs === 0 ? skew : ws.clockSkewMs * 0.8 + skew * 0.2;
  }
  function LP() { return global.LeadPanel; }

  /* ---- preferences -------------------------------------------------- */

  function loadPrefs() {
    try {
      var raw = localStorage.getItem(PREFS_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && parsed.ema) ws.prefs = parsed;
      }
    } catch (e) { /* defaults are fine */ }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(ws.prefs)); } catch (e) {}
  }

  /* ---- chart -------------------------------------------------------- */

  function buildChart() {
    var container = el('wsChart');
    ws.chart = LightweightCharts.createChart(container, {
      layout: { background: { color: '#0e1117' }, textColor: '#c7cbd3' },
      grid: { vertLines: { color: '#161b24' }, horzLines: { color: '#161b24' } },
      rightPriceScale: { borderColor: '#232733', scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: '#232733', timeVisible: true, secondsVisible: false },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true,
                      vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
      localization: { priceFormatter: function (p) { return global.LeadFib.formatPrice(p); } }
    });
    ws.series = ws.chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350'
    });
    ws.volume = ws.chart.addHistogramSeries({
      priceFormat: { type: 'volume' }, priceScaleId: 'vol',
      color: '#233043'
    });
    ws.chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    new ResizeObserver(function () {
      if (container.clientWidth) {
        ws.chart.resize(container.clientWidth, container.clientHeight);
      }
    }).observe(container);

    ws.fib = new global.LeadFib.FibTool({
      chart: ws.chart, series: ws.series, container: container,
      symbol: ws.symbol, timeframe: ws.timeframe,
      ratios: global.LeadFib.ALL,
      onChange: renderFibState
    });
  }

  function emaLine(period) {
    if (ws.emaSeries[period]) return ws.emaSeries[period];
    ws.emaSeries[period] = ws.chart.addLineSeries({
      color: EMA_COLORS[period], lineWidth: period >= 50 ? 2 : 1,
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
      title: 'EMA ' + period
    });
    return ws.emaSeries[period];
  }

  /* EMA, computed locally from OHLC closes. Standard formula, seeded with
     a simple mean, identical to the server's `candles_rest.ema` so the two
     can be checked against each other. */
  function ema(values, period) {
    if (period <= 0 || values.length < period) return values.map(function () { return null; });
    var alpha = 2 / (period + 1);
    var out = [];
    var i;
    for (i = 0; i < period - 1; i++) out.push(null);
    var seed = 0;
    for (i = 0; i < period; i++) seed += values[i];
    seed /= period;
    out.push(seed);
    var previous = seed;
    for (i = period; i < values.length; i++) {
      previous = alpha * values[i] + (1 - alpha) * previous;
      out.push(previous);
    }
    return out;
  }
  ws.ema = ema;

  function drawEmas() {
    Object.keys(EMA_COLORS).forEach(function (period) {
      var on = ws.prefs.ema[period];
      var line = ws.emaSeries[period];
      if (!on) {
        if (line) { ws.chart.removeSeries(line); delete ws.emaSeries[period]; }
        return;
      }
      line = emaLine(period);
      var values = ema(ws.closes, Number(period));
      var points = [];
      for (var i = 0; i < values.length; i++) {
        if (values[i] !== null) points.push({ time: ws.candles[i].time, value: values[i] });
      }
      line.setData(points);
      // Remember where the recursion stands at the last CLOSED bar, so
      // the forming bar can be extended from it without redoing any of
      // this. See extendEmas.
      var anchorIndex = values.length - 2;
      ws.emaAnchor[period] = (anchorIndex >= 0 && values[anchorIndex] !== null)
        ? { value: values[anchorIndex], index: anchorIndex } : null;
    });
  }

  /* The EMA of the FORMING bar, from the previous value.

     EMA is a recursion - EMA_t = a*close_t + (1-a)*EMA_(t-1) - so
     extending it costs one multiply. The previous version called the
     full `ema(ws.closes, period)` here instead: six hundred bars, four
     periods, twice a second, to use the last element of each and throw
     away the rest.

     The anchor is the EMA at the last CLOSED bar, never at the last tick.
     Compounding within a bar - feeding each tick's result back in - would
     make the line depend on how often the page happened to poll. */
  function extendEmas() {
    var last = ws.candles[ws.candles.length - 1];
    if (!last) return;
    var close = ws.closes[ws.closes.length - 1];
    Object.keys(ws.emaSeries).forEach(function (key) {
      var period = Number(key);
      var anchor = ws.emaAnchor[key];
      if (!anchor) {
        // No anchor yet (history just arrived, or too few bars). Pay for
        // one full pass and keep the result.
        var values = ema(ws.closes, period);
        var index = values.length - 2;
        if (index < 0 || values[index] === null) return;
        anchor = ws.emaAnchor[key] = { value: values[index], index: index };
      }
      var alpha = 2 / (period + 1);
      var value = alpha * close + (1 - alpha) * anchor.value;
      ws.emaSeries[key].update({ time: last.time, value: value });
    });
  }

  /* A bar just closed: the value that was provisional becomes the anchor.
     Called when a new bar is appended, from either source. */
  function commitEmaBar() {
    var closedIndex = ws.closes.length - 2;
    if (closedIndex < 0) return;
    var closedValue = ws.closes[closedIndex];
    Object.keys(ws.emaAnchor).forEach(function (key) {
      var anchor = ws.emaAnchor[key];
      if (!anchor) return;
      if (anchor.index >= closedIndex) return;   // already committed
      var alpha = 2 / (Number(key) + 1);
      ws.emaAnchor[key] = { value: alpha * closedValue + (1 - alpha) * anchor.value,
                            index: closedIndex };
    });
  }

  /* ---- data --------------------------------------------------------- */

  function get(path) {
    var began = performance.now();
    return fetch(path, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        ws.uiLatencyMs = performance.now() - began;
        ws.fetches += 1;
        return data;
      }, function (e) {
        // Marked, so a caller can tell a dropped poll (ignorable) from a
        // bug in its own success path (not ignorable).
        var err = e instanceof Error ? e : new Error(String(e));
        err.__fetch = true;
        throw err;
      });
  }

  function candleUrl(limit) {
    return '/api/lead-engine/candles/' + encodeURIComponent(ws.symbol) +
           '?timeframe=' + encodeURIComponent(ws.timeframe) + '&limit=' + limit;
  }

  function adopt(row, fromExchange) {
    return { time: row.time, open: row.open, high: row.high, low: row.low,
             close: row.close, volume: row.volume, closed: row.closed,
             // Whether Bybit produced this bar or the browser did. Only
             // an exchange bar may draw a volume: see drawVolume.
             exchange: fromExchange !== false };
  }

  function loadHistory() {
    var note = el('wsChartNote');
    var token = ++ws.generation;
    note.textContent = 'Loading ' + ws.symbol + ' ' + ws.timeframe + '…';
    return get(candleUrl(600))
      .then(function (data) {
        if (token !== ws.generation) return;     // a newer timeframe won
        var rows = (data && data.candles) || [];
        if (!rows.length) {
          note.textContent = 'No candles returned for ' + ws.symbol + ' ' + ws.timeframe +
            (data && data.detail ? ' — ' + data.detail : '');
          return;
        }
        ws.candles = rows.map(function (row) { return adopt(row, true); });
        ws.closes = ws.candles.map(function (c) { return c.close; });
        // ONE setData per (symbol, timeframe). Everything after this is
        // update() on the forming bar.
        ws.series.setData(ws.candles.map(function (c) {
          return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
        }));
        ws.setDataCalls += 1;
        drawVolume();
        drawEmas();
        ws.chart.timeScale().fitContent();
        note.textContent = rows.length + ' candles · ' + ws.timeframe +
          ' · history loaded once, live bar refreshed from Bybit';
      })
      .catch(function (e) {
        if (token !== ws.generation) return;
        note.textContent = 'Could not load candles: ' + e.message;
      });
  }

  /* The forming bar, from the exchange.

     This exists because the first version BUILT the live bar out of a
     price polled every 500ms and the browser's own clock. Three things
     were wrong with that and all three are silent:

       - every high and low between two polls was lost, so a spike that
         lasted two seconds never appeared on the chart at all;
       - the bar's volume was whatever the browser had accumulated, which
         was nothing: the one call site passed null, so the live bar's
         volume was permanently zero;
       - the bar boundary came from `Date.now()`, so a browser clock a
         minute out put every tick in the wrong bar.

     Bybit's own kline carries the true high, low and volume for the bar
     still forming, and `fetch_candles` already marks it `closed: false`.
     So it is fetched, not invented. Between refreshes a live price may
     only EXTEND the bar - push the close, raise a high, lower a low -
     because those are things price genuinely did; it may never shrink a
     range or invent a volume. */
  /* The forming bar from the engine's own kline stream.

     Preferred over the REST refresh below because it is what Bybit is
     pushing right now - aggregated server-side from `kline.1`, so it
     carries the true high, low, close and volume, and says how many of
     its minutes the exchange has confirmed. REST remains the fallback
     when the engine has no kline data for this bar yet. */
  function refreshFromKline() {
    if (!ws.candles.length) return Promise.resolve(false);
    var token = ws.generation;
    return get('/api/lead-engine/live-candle/' + encodeURIComponent(ws.symbol) +
               '?timeframe=' + encodeURIComponent(ws.timeframe))
      .then(function (data) {
        if (token !== ws.generation) return false;
        var row = data && data.candle;
        if (!row) return false;
        var last = ws.candles[ws.candles.length - 1];
        var bar = adopt(row, true);
        if (row.time === last.time) {
          ws.candles[ws.candles.length - 1] = bar;
          ws.closes[ws.closes.length - 1] = bar.close;
        } else if (row.time > last.time) {
          ws.candles.push(bar);
          ws.closes.push(bar.close);
          if (ws.candles.length > 1500) { ws.candles.shift(); ws.closes.shift(); }
          commitEmaBar();
        } else {
          return false;
        }
        ws.series.update({ time: bar.time, open: bar.open, high: bar.high,
                           low: bar.low, close: bar.close });
        if (ws.prefs.volume) {
          ws.volume.update({ time: bar.time, value: bar.volume || 0,
                             color: bar.close >= bar.open ? '#1d4b45' : '#4b2020' });
        }
        ws.klineRefreshes = (ws.klineRefreshes || 0) + 1;
        extendEmas();
        return true;
      })
      .catch(function () { return false; });
  }

  function refreshLiveBars() {
    if (!ws.candles.length) return Promise.resolve();
    // Kline first; REST only when the engine has nothing for this bar.
    return refreshFromKline().then(function (served) {
      return served ? null : refreshLiveBarsFromRest();
    });
  }

  function refreshLiveBarsFromRest() {
    if (!ws.candles.length) return Promise.resolve();
    var token = ws.generation;
    return get(candleUrl(3))
      .then(function (data) {
        if (token !== ws.generation) return;     // the timeframe changed
        var rows = (data && data.candles) || [];
        if (!rows.length) return;
        rows.forEach(function (row) {
          var index = -1;
          for (var i = ws.candles.length - 1; i >= 0 && i > ws.candles.length - 8; i--) {
            if (ws.candles[i].time === row.time) { index = i; break; }
          }
          var bar = adopt(row, true);
          if (index >= 0) {
            // The exchange is authoritative for a bar it has sent, INCLUDING
            // shrinking a range this browser extended from a stray tick.
            ws.candles[index] = bar;
            ws.closes[index] = bar.close;
          } else if (row.time > ws.candles[ws.candles.length - 1].time) {
            ws.candles.push(bar);
            ws.closes.push(bar.close);
            if (ws.candles.length > 1500) { ws.candles.shift(); ws.closes.shift(); }
            commitEmaBar();
          } else {
            return;                              // older than the window
          }
          ws.series.update({ time: bar.time, open: bar.open, high: bar.high,
                             low: bar.low, close: bar.close });
          if (ws.prefs.volume) {
            ws.volume.update({ time: bar.time, value: bar.volume || 0,
                               color: bar.close >= bar.open ? '#1d4b45' : '#4b2020' });
          }
        });
        ws.liveRefreshes = (ws.liveRefreshes || 0) + 1;
        extendEmas();
      })
      .catch(function () { /* one dropped refresh; the next one carries it */ });
  }

  function drawVolume() {
    if (!ws.prefs.volume) { ws.volume.setData([]); return; }
    // Only bars the EXCHANGE sent get a volume. A provisional bar opened
    // by this browser has no volume to report, and drawing a zero for it
    // would be a claim rather than a gap.
    ws.volume.setData(ws.candles
      .filter(function (c) { return c.exchange !== false && c.volume !== null; })
      .map(function (c) {
        return { time: c.time, value: c.volume || 0,
                 color: c.close >= c.open ? '#1d4b45' : '#4b2020' };
      }));
  }

  /* One live price into the forming bar. EXTEND ONLY.

     What this may do: move the close, raise a high, lower a low - all
     things price genuinely did between two refreshes from the exchange.

     What it may not do: invent a volume, shrink a range, or decide on
     its own that a bar has closed. The bar itself comes from Bybit (see
     refreshLiveBars); this only keeps it moving in between.

     It also refuses to touch the chart when the feed is stale. A price
     that is four seconds old is not a live price, and painting it onto
     the current bar is how a chart comes to disagree with the market
     while looking perfectly healthy. */
  function applyTick(price) {
    if (!price || !ws.candles.length) return;
    if (ws.liveStale) return;

    var seconds = TF_SECONDS[ws.timeframe] || 300;
    var bucket = Math.floor(Math.floor(exchangeNow() / 1000) / seconds) * seconds;
    var last = ws.candles[ws.candles.length - 1];

    if (bucket > last.time) {
      // A bar boundary. Provisional, and marked as such: it carries no
      // volume and no true open until the exchange sends this bar, which
      // refreshLiveBars will then use to replace it outright.
      last.closed = true;
      var fresh = { time: bucket, open: price, high: price, low: price,
                    close: price, volume: null, closed: false, exchange: false };
      ws.candles.push(fresh);
      ws.closes.push(price);
      if (ws.candles.length > 1500) { ws.candles.shift(); ws.closes.shift(); }
      commitEmaBar();
      ws.series.update({ time: fresh.time, open: fresh.open, high: fresh.high,
                         low: fresh.low, close: fresh.close });
      // Ask the exchange for the real one now rather than at the next tick.
      refreshLiveBars();
    } else if (bucket === last.time) {
      last.high = Math.max(last.high, price);
      last.low = Math.min(last.low, price);
      last.close = price;
      ws.closes[ws.closes.length - 1] = price;
      ws.series.update({ time: last.time, open: last.open, high: last.high,
                         low: last.low, close: last.close });
    } else {
      return;                       // a stale price for a bar already closed
    }
    ws.updates += 1;
    extendEmas();
  }

  /* ---- signal markers ----------------------------------------------- */

  function updateMarkers(frame) {
    if (!ws.prefs.signals) { ws.series.setMarkers([]); return; }
    var signal = frame.signal || {};
    var interesting = ['PRE_BREAK_LONG', 'PRE_BREAK_SHORT', 'HIGH_PROBABILITY',
                       'A_PLUS', 'REVERSAL_CANDIDATE'];
    if (interesting.indexOf(signal.state) === -1) return;
    // The signal's OWN timestamp, snapped to the bar it happened in. The
    // marker is never moved afterwards — a marker relocated to a better
    // pivot makes every backtest built on this chart worthless.
    var seconds = TF_SECONDS[ws.timeframe] || 300;
    var at = Math.floor(Number(signal.changed_at || Date.now() / 1000));
    var bucket = Math.floor(at / seconds) * seconds;
    var id = signal.state + '@' + bucket;
    if (ws.markers.some(function (m) { return m.id === id; })) return;
    ws.markers.push({
      id: id, time: bucket,
      position: signal.direction === 'long' ? 'belowBar' : 'aboveBar',
      color: signal.direction === 'long' ? '#34d399' : '#f87171',
      shape: signal.direction === 'long' ? 'arrowUp' : 'arrowDown',
      text: signal.state.replace(/_/g, ' ')
    });
    if (ws.markers.length > 60) ws.markers.shift();
    ws.series.setMarkers(ws.markers.slice().sort(function (a, b) { return a.time - b.time; }));
  }

  /* ---- panel -------------------------------------------------------- */

  function mountPanel() {
    var body = el('wsPanelBody');
    if (ws.panel) return;
    // Parenthesised deliberately: `new LP().Panel(...)` parses as
    // `(new LP()).Panel(...)`, which calls Panel as a method instead of a
    // constructor and quietly yields undefined.
    ws.panel = new (LP().Panel)(body, { refreshMs: REFRESH_MS });
    ws.panel.paint = function (frame) {
      global.LeadBlocks.applyFrame(ws.panel, frame, { uiLatencyMs: ws.uiLatencyMs });
    };
    body.innerHTML = global.LeadBlocks.buildBlocks(ws.panel);
    ws.panel.collect();
  }

  function loadFrame() {
    return get('/api/lead-engine/state/' + encodeURIComponent(ws.symbol))
      .then(function (data) {
        if (!data || data.enabled === false) {
          el('wsChartNote').textContent = 'The Lead Engine is switched off.';
          return;
        }
        ws.frame = data;
        noteServerClock(data);
        // Whether this price may touch the chart at all. A price the
        // engine itself will not act on is not a price to draw with.
        var health = data.health || {};
        var bookAge = Number(health.book_age_ms);
        ws.liveStale = health.signals_enabled === false ||
                       (isFinite(bookAge) && bookAge > STALE_DRAW_MS);
        mountPanel();
        ws.panel.push(data);
        renderHeader(data);
        applyTick(Number(data.price));
        updateMarkers(data);
      })
      .catch(function (e) {
        // A dropped poll is not worth shouting about, but a THROW inside
        // the success path is - swallowing it leaves the page sitting at
        // "—" with no clue why.
        if (e && e.__fetch) return;
        if (global.console) console.error('lead workspace frame failed', e);
        var note = el('wsChartNote');
        if (note) note.textContent = 'Frame update failed: ' + (e && e.message || e);
      });
  }

  function renderHeader(frame) {
    var health = frame.health || {};
    var signal = frame.signal || {};
    var price = el('wsPrice');
    var text = global.LeadFib.formatPrice(Number(frame.price) || 0);
    if (price.textContent !== text) price.textContent = text;
    var state = el('wsState');
    if (state.textContent !== signal.state) {
      state.textContent = signal.state || 'IDLE';
      state.className = 'le-state ' + global.LeadBlocks.stateClass(signal.state);
    }
    var quality = el('wsQuality');
    var qualityText = 'feed ' + (health.status || '—');
    if (quality.textContent !== qualityText) {
      quality.textContent = qualityText;
      quality.className = 'le-chip ' + (health.status === 'OK' ? 'ok' : 'bad');
    }
  }

  /* ---- controls ----------------------------------------------------- */

  function renderTimeframes() {
    var row = el('wsTimeframes');
    row.innerHTML = TIMEFRAMES.map(function (tf) {
      return '<button data-tf="' + tf + '"' + (tf === ws.timeframe ? ' class="active"' : '') +
        '>' + tf + '</button>';
    }).join('');
    Array.prototype.forEach.call(row.querySelectorAll('[data-tf]'), function (btn) {
      btn.addEventListener('click', function () { setTimeframe(btn.getAttribute('data-tf')); });
    });
  }

  function setTimeframe(tf) {
    if (tf === ws.timeframe) return;
    ws.timeframe = tf;
    // The bars on screen belong to the OLD timeframe. Cleared here rather
    // than left to be overwritten, so a slow history response cannot find
    // 1m bars sitting under a 15m request and merge the two.
    ws.candles = [];
    ws.closes = [];
    ws.markers = [];
    ws.series.setMarkers([]);
    renderTimeframes();
    // Drawings are per symbol AND timeframe: switching swaps the set on
    // screen, it never mixes them.
    ws.fib.setChart(ws.symbol, ws.timeframe);
    // loadHistory() bumps the generation, so any response still in flight
    // for the previous timeframe is discarded when it lands.
    loadHistory();
  }

  function renderFibState() {
    var hint = el('wsFibHint');
    if (!hint || !ws.fib) return;
    var state = ws.fib.state();
    hint.textContent = state.armed
      ? (state.pending ? 'Now tap the END of the move.' : 'Tap the START of the move.')
      : state.drawings + ' drawing(s) on ' + state.symbol + ' ' + state.timeframe +
        (state.hidden ? ' (hidden)' : '') +
        '. Tap Draw, then two points on the chart.';
    var button = el('wsFib');
    if (button) button.classList.toggle('active', state.armed);
  }

  function buildFibRatios() {
    var box = el('wsFibRatios');
    box.innerHTML = global.LeadFib.ALL.map(function (r) {
      var on = global.LeadFib.RETRACEMENTS.indexOf(r) !== -1;
      return '<label><input type="checkbox" data-ratio="' + r + '"' +
        (on ? ' checked' : '') + ' /> ' + r + '</label>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('[data-ratio]'), function (input) {
      input.addEventListener('change', function () {
        var chosen = [];
        Array.prototype.forEach.call(box.querySelectorAll('[data-ratio]'), function (node) {
          if (node.checked) chosen.push(Number(node.getAttribute('data-ratio')));
        });
        ws.fib.setRatios(chosen);
      });
    });
  }

  function bindControls() {
    var indicators = el('wsIndicators');
    var menu = el('wsIndicatorMenu');
    indicators.addEventListener('click', function () {
      menu.hidden = !menu.hidden;
      el('wsFibMenu').hidden = true;
    });
    Array.prototype.forEach.call(menu.querySelectorAll('[data-ema]'), function (input) {
      var period = input.getAttribute('data-ema');
      input.checked = !!ws.prefs.ema[period];
      input.addEventListener('change', function () {
        ws.prefs.ema[period] = input.checked;
        savePrefs();
        drawEmas();
      });
    });
    el('wsShowVolume').checked = ws.prefs.volume;
    el('wsShowVolume').addEventListener('change', function () {
      ws.prefs.volume = el('wsShowVolume').checked;
      savePrefs();
      drawVolume();
    });
    el('wsShowSignals').checked = ws.prefs.signals;
    el('wsShowSignals').addEventListener('change', function () {
      ws.prefs.signals = el('wsShowSignals').checked;
      savePrefs();
      if (!ws.prefs.signals) ws.series.setMarkers([]);
    });

    var fibButton = el('wsFib');
    var fibMenu = el('wsFibMenu');
    fibButton.addEventListener('click', function () {
      fibMenu.hidden = !fibMenu.hidden;
      menu.hidden = true;
      renderFibState();
    });
    el('wsFibDraw').addEventListener('click', function () { ws.fib.arm(true); renderFibState(); });
    el('wsFibHide').addEventListener('click', function () { ws.fib.toggleHidden(); });
    el('wsFibDelete').addEventListener('click', function () { ws.fib.deleteLast(); });
    el('wsFibReset').addEventListener('click', function () { ws.fib.reset(); });

    el('wsFullscreen').addEventListener('click', function () {
      document.body.classList.toggle('ws-fullscreen');
      setTimeout(function () {
        var container = el('wsChart');
        ws.chart.resize(container.clientWidth, container.clientHeight);
      }, 60);
    });

    Array.prototype.forEach.call(document.querySelectorAll('#wsMobileTabs button'),
      function (button) {
        button.addEventListener('click', function () {
          var pane = button.getAttribute('data-pane');
          Array.prototype.forEach.call(document.querySelectorAll('#wsMobileTabs button'),
            function (b) { b.classList.toggle('active', b === button); });
          el('wsChartPane').classList.toggle('hidden', pane !== 'chart');
          el('wsPanel').classList.toggle('hidden', pane !== 'panel');
          if (pane === 'chart') {
            var container = el('wsChart');
            ws.chart.resize(container.clientWidth, container.clientHeight);
          }
        });
      });
  }

  /* ---- boot --------------------------------------------------------- */

  function start() {
    var parts = location.pathname.split('/').filter(Boolean);
    ws.symbol = (parts[parts.length - 1] || 'INJUSDT').toUpperCase();
    var query = new URLSearchParams(location.search);
    ws.timeframe = query.get('tf') || ws.timeframe;
    document.title = ws.symbol + ' — Market Lead Engine';
    el('wsSymbol').textContent = ws.symbol;

    loadPrefs();
    buildChart();
    buildFibRatios();
    renderTimeframes();
    bindControls();
    ws.fib.setChart(ws.symbol, ws.timeframe);
    loadHistory().then(loadFrame);
    ws.timer = setInterval(loadFrame, FETCH_MS);
    ws.liveTimer = setInterval(refreshLiveBars, LIVE_BAR_MS);
  }

  ws.stats = function () {
    return { fetches: ws.fetches, chartUpdates: ws.updates, setDataCalls: ws.setDataCalls,
             liveRefreshes: ws.liveRefreshes || 0,
             klineRefreshes: ws.klineRefreshes || 0,
             clockSkewMs: Math.round(ws.clockSkewMs),
             liveStale: ws.liveStale, generation: ws.generation,
             candles: ws.candles.length,
             panel: ws.panel ? ws.panel.stats() : null };
  };

  document.addEventListener('DOMContentLoaded', start);
})(window);
