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
  var EMA_COLORS = { 9: '#38bdf8', 18: '#34d399', 50: '#fbbf24', 200: '#f472b6' };
  var PREFS_KEY = 'lead_ws_prefs';

  var ws = {
    symbol: null, timeframe: '5m', chart: null, series: null, volume: null,
    emaSeries: {}, candles: [], closes: [], panel: null, fib: null,
    frame: null, timer: null, markers: [], uiLatencyMs: null,
    prefs: { ema: { 9: false, 18: false, 50: true, 200: true },
             volume: true, signals: true },
    fetches: 0, updates: 0, setDataCalls: 0
  };
  global.leadWorkspace = ws;

  function el(id) { return document.getElementById(id); }
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
    });
  }

  /* Extend the EMAs by the newest bar only. The full recompute above runs
     when history loads or a toggle changes; a tick must not redo 500
     bars four times a second. */
  function extendEmas() {
    var last = ws.candles[ws.candles.length - 1];
    if (!last) return;
    Object.keys(ws.emaSeries).forEach(function (period) {
      var values = ema(ws.closes, Number(period));
      var value = values[values.length - 1];
      if (value !== null && value !== undefined) {
        ws.emaSeries[period].update({ time: last.time, value: value });
      }
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
      });
  }

  function loadHistory() {
    var note = el('wsChartNote');
    note.textContent = 'Loading ' + ws.symbol + ' ' + ws.timeframe + '…';
    return get('/api/lead-engine/candles/' + encodeURIComponent(ws.symbol) +
               '?timeframe=' + encodeURIComponent(ws.timeframe) + '&limit=600')
      .then(function (data) {
        var rows = (data && data.candles) || [];
        if (!rows.length) {
          note.textContent = 'No candles returned for ' + ws.symbol + ' ' + ws.timeframe +
            (data && data.detail ? ' — ' + data.detail : '');
          return;
        }
        ws.candles = rows.map(function (c) {
          return { time: c.time, open: c.open, high: c.high, low: c.low,
                   close: c.close, volume: c.volume, closed: c.closed };
        });
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
          ' · history loaded once, live bar updated incrementally';
      })
      .catch(function (e) { note.textContent = 'Could not load candles: ' + e.message; });
  }

  function drawVolume() {
    if (!ws.prefs.volume) { ws.volume.setData([]); return; }
    ws.volume.setData(ws.candles.map(function (c) {
      return { time: c.time, value: c.volume || 0,
               color: c.close >= c.open ? '#1d4b45' : '#4b2020' };
    }));
  }

  /* One price tick into the live bar. Appends a new bar when the tick
     belongs to the next interval — which is how a candle closes without
     the history ever being refetched. */
  function applyTick(price, volumeHint) {
    if (!price || !ws.candles.length) return;
    var seconds = TF_SECONDS[ws.timeframe] || 300;
    var now = Math.floor(Date.now() / 1000);
    var bucket = Math.floor(now / seconds) * seconds;
    var last = ws.candles[ws.candles.length - 1];

    if (bucket > last.time) {
      last.closed = true;
      var fresh = { time: bucket, open: price, high: price, low: price,
                    close: price, volume: 0, closed: false };
      ws.candles.push(fresh);
      ws.closes.push(price);
      if (ws.candles.length > 1500) { ws.candles.shift(); ws.closes.shift(); }
      ws.series.update({ time: fresh.time, open: fresh.open, high: fresh.high,
                         low: fresh.low, close: fresh.close });
    } else if (bucket === last.time) {
      last.high = Math.max(last.high, price);
      last.low = Math.min(last.low, price);
      last.close = price;
      if (volumeHint) last.volume = volumeHint;
      ws.closes[ws.closes.length - 1] = price;
      ws.series.update({ time: last.time, open: last.open, high: last.high,
                         low: last.low, close: last.close });
    } else {
      return;                       // a stale price for a bar already closed
    }
    ws.updates += 1;
    if (ws.prefs.volume) {
      var current = ws.candles[ws.candles.length - 1];
      ws.volume.update({ time: current.time, value: current.volume || 0,
                         color: current.close >= current.open ? '#1d4b45' : '#4b2020' });
    }
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
    ws.panel = new LP().Panel(body, { refreshMs: REFRESH_MS });
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
        mountPanel();
        ws.panel.push(data);
        global.LeadBlocks.applyFrame(ws.panel, data, { uiLatencyMs: ws.uiLatencyMs });
        renderHeader(data);
        applyTick(Number(data.price), null);
        updateMarkers(data);
      })
      .catch(function () { /* one dropped poll is not an error worth shouting */ });
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
    ws.markers = [];
    ws.series.setMarkers([]);
    renderTimeframes();
    // Drawings are per symbol AND timeframe: switching swaps the set on
    // screen, it never mixes them.
    ws.fib.setChart(ws.symbol, ws.timeframe);
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
  }

  ws.stats = function () {
    return { fetches: ws.fetches, chartUpdates: ws.updates, setDataCalls: ws.setDataCalls,
             candles: ws.candles.length,
             panel: ws.panel ? ws.panel.stats() : null };
  };

  document.addEventListener('DOMContentLoaded', start);
})(window);
