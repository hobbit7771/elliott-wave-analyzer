/* Market Lead Engine — the tab.
 *
 * Rewritten. The first version rebuilt eleven cards of HTML and assigned
 * them to `innerHTML` every second, which destroyed and recreated every
 * label, bar and number once a second. That is the jitter.
 *
 * Now: `LeadBlocks.buildBlocks` renders the markup ONCE at mount, and
 * `LeadBlocks.applyFrame` writes only the values that changed, on a
 * throttled cadence (`LeadPanel.Panel`). Calculation frequency and
 * display refresh frequency are separate knobs — the engine recalculates
 * as fast as it likes, the panel repaints at most every 300ms, and the
 * newest state replaces any pending one instead of queueing behind it.
 *
 * Polling only while visible, as before: an unopened tab costs nothing.
 */
(function (global) {
  'use strict';

  var FETCH_MS = 500;          // how often state is pulled
  var STATUS_MS = 4000;
  var REFRESH_MS = 300;        // how often the panel may repaint

  var store = {
    symbol: null, symbols: [], enabled: null, frame: null, status: null,
    open: false, timer: null, statusTimer: null, error: '',
    panel: null, uiLatencyMs: null, fetches: 0
  };
  global.leadEngineStore = store;

  function el(id) { return document.getElementById(id); }
  function esc(t) { return global.LeadPanel.esc(t); }

  function get(path) {
    var began = performance.now();
    return fetch(path, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // UI latency: request out to render-ready, measured in the
        // browser because the server cannot know it.
        store.uiLatencyMs = performance.now() - began;
        store.fetches += 1;
        return data;
      });
  }

  function mount() {
    var body = el('leBody');
    if (!body || store.panel) return;
    store.panel = new global.LeadPanel.Panel(body, { refreshMs: REFRESH_MS });
    body.innerHTML = global.LeadBlocks.buildBlocks(store.panel);
    store.panel.collect();
  }

  function renderSymbolPicker() {
    var picker = el('leSymbol');
    if (!picker) return;
    if (picker.options.length !== store.symbols.length) {
      picker.innerHTML = store.symbols.map(function (s) {
        return '<option value="' + esc(s) + '">' + esc(s) + '</option>';
      }).join('');
    }
    if (store.symbol) picker.value = store.symbol;
    var open = el('leOpen');
    if (open) open.textContent = 'OPEN CHART — ' + (store.symbol || '');
  }

  function renderFleet() {
    var target = el('leFleet');
    if (!target || !store.status || !store.status.enabled) return;
    var rows = (store.status.symbols || []).map(function (s) {
      return '<tr class="' + (s.symbol === store.symbol ? 'me' : '') + '">' +
        '<td><span class="le-open" data-open="' + esc(s.symbol) + '">' +
        esc(s.symbol) + '</span></td>' +
        '<td class="le-num">' + global.LeadPanel.fmt(s.price, 4) + '</td>' +
        '<td>' + esc(s.state) + '</td>' +
        '<td class="le-num">' + global.LeadPanel.fmt(s.long_pressure, 0) + '</td>' +
        '<td class="le-num">' + global.LeadPanel.fmt(s.short_pressure, 0) + '</td>' +
        '<td>' + esc(s.health) + '</td></tr>';
    }).join('');
    var html = '<table class="le-table"><thead><tr><th>Symbol</th><th>Price</th>' +
      '<th>State</th><th>Long</th><th>Short</th><th>Feed</th></tr></thead><tbody>' +
      rows + '</tbody></table>';
    // The fleet table changes shape rarely; rebuilding it only when the
    // markup actually differs keeps it out of the per-tick path.
    if (target.__html !== html) { target.innerHTML = html; target.__html = html; bindFleet(); }
  }

  function bindFleet() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-open]'), function (node) {
      node.addEventListener('click', function () { openWorkspace(node.getAttribute('data-open')); });
    });
  }

  function openWorkspace(symbol) {
    if (!symbol) return;
    global.open('/lead-engine/' + encodeURIComponent(symbol), '_blank');
  }

  function renderHead(frame) {
    var head = el('leHead');
    if (!head) return;
    var health = frame.health || {};
    var stream = (store.status && store.status.stream) || {};
    if (!head.__built) {
      head.innerHTML =
        '<span class="le-price le-num" data-le="hd:price">—</span>' +
        '<span class="le-chip le-open" id="leHeadSymbol">—</span>' +
        '<span class="le-chip" data-le="hd:feed">feed —</span>' +
        '<span class="le-chip" data-le="hd:ws">ws —</span>' +
        '<span class="le-chip" data-le="hd:signals">signals —</span>';
      head.__built = true;
      var node = el('leHeadSymbol');
      if (node) node.addEventListener('click', function () { openWorkspace(store.symbol); });
      store.panel.collect();
    }
    store.panel.set('hd:price', global.LeadPanel.fmt(frame.price, 5));
    store.panel.set('hd:feed', 'feed ' + (health.status || '—'));
    store.panel.klass('hd:feed', 'le-chip ' + (health.status === 'OK' ? 'ok' : 'bad'));
    store.panel.set('hd:ws', stream.connected ? 'ws up' : 'ws down');
    store.panel.klass('hd:ws', 'le-chip ' + (stream.connected ? 'ok' : 'bad'));
    store.panel.set('hd:signals', health.signals_enabled ? 'signals on' : 'signals off');
    store.panel.klass('hd:signals', 'le-chip ' + (health.signals_enabled ? 'ok' : 'bad'));
    var symbolChip = el('leHeadSymbol');
    if (symbolChip && symbolChip.textContent !== store.symbol) {
      symbolChip.textContent = store.symbol || '—';
    }
  }

  function renderOff() {
    var body = el('leBody');
    if (!body) return;
    var detail = (store.status && store.status.detail) ||
      'The Market Lead Engine is switched off.';
    body.innerHTML = '<div class="le-off"><strong>Engine off.</strong><br>' + esc(detail) +
      '<br><br>While it is off this tab makes no repeated requests, no WebSocket is ' +
      'opened, no thread is started and no polling happens.</div>';
    store.panel = null;
    var head = el('leHead');
    if (head) { head.innerHTML = '<span class="le-chip warn">ENGINE OFF</span>'; head.__built = false; }
  }

  function loadSymbols() {
    return get('/api/lead-engine/symbols').then(function (data) {
      store.enabled = !!data.enabled;
      store.symbols = data.symbols || [];
      if (!store.symbol && store.symbols.length) store.symbol = store.symbols[0];
      renderSymbolPicker();
      return data;
    });
  }

  function loadStatus() {
    return get('/api/lead-engine/status').then(function (data) {
      store.status = data;
      store.enabled = !!data.enabled;
      if (data.enabled) renderFleet(); else renderOff();
    }).catch(function (e) { store.error = e.message || 'status failed'; });
  }

  function loadFrame() {
    if (!store.symbol) return Promise.resolve();
    return get('/api/lead-engine/state/' + encodeURIComponent(store.symbol))
      .then(function (data) {
        store.enabled = !!data.enabled;
        if (!data.enabled) { renderOff(); return; }
        store.frame = data;
        store.error = '';
        mount();
        renderHead(data);
        // Coalesced: the newest frame replaces any pending one, and the
        // panel paints at most every REFRESH_MS.
        store.panel.push(data);
        global.LeadBlocks.applyFrame(store.panel, data, { uiLatencyMs: store.uiLatencyMs });
      })
      .catch(function (e) { store.error = e.message || 'state failed'; });
  }

  function tick() {
    if (!store.open) return;
    if (store.enabled === false) return;
    loadFrame();
  }

  store.show = function () {
    store.open = true;
    loadSymbols().then(function () { loadStatus(); loadFrame(); })
      .catch(function (e) { store.error = e.message || 'engine unreachable'; });
    if (store.timer) clearInterval(store.timer);
    if (store.statusTimer) clearInterval(store.statusTimer);
    store.timer = setInterval(tick, FETCH_MS);
    store.statusTimer = setInterval(function () {
      if (store.open && store.enabled !== false) loadStatus();
    }, STATUS_MS);
  };

  store.hide = function () {
    store.open = false;
    if (store.timer) { clearInterval(store.timer); store.timer = null; }
    if (store.statusTimer) { clearInterval(store.statusTimer); store.statusTimer = null; }
  };

  store.setSymbol = function (symbol) {
    store.symbol = symbol;
    store.frame = null;
    renderSymbolPicker();
    loadFrame();
  };

  store.panelStats = function () { return store.panel ? store.panel.stats() : null; };

  document.addEventListener('DOMContentLoaded', function () {
    var picker = el('leSymbol');
    if (picker) picker.addEventListener('change', function () { store.setSymbol(picker.value); });
    var refresh = el('leRefresh');
    if (refresh) refresh.addEventListener('click', function () { loadStatus(); loadFrame(); });
    var open = el('leOpen');
    if (open) open.addEventListener('click', function () { openWorkspace(store.symbol); });
  });
})(window);
