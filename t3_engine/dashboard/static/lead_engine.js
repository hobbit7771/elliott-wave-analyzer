/* Market Lead Engine - the tab's entire front end, in its own file.
 *
 * It shares exactly three things with the rest of the page: the tab
 * button, the panel div it renders into, and the browser. It defines one
 * global, `leadEngineStore`, and reads no state belonging to any other
 * tab. Nothing in index.html's own script needs to know this file exists
 * beyond calling leadEngineStore.open() when the tab is shown.
 *
 * Polling, not a socket: the exchange socket is on the server, and a
 * second one from the browser would be a second subscription to the same
 * data with no way to keep the two in step. The tab asks for a frame,
 * renders it, and asks again - and it only polls while it is VISIBLE,
 * which is the specification's requirement that an unopened tab costs
 * nothing.
 */
(function () {
  'use strict';

  var POLL_MS = 1000;
  var STATUS_POLL_MS = 4000;

  var store = {
    symbol: null,
    symbols: [],
    enabled: null,
    frame: null,
    status: null,
    open: false,
    timer: null,
    statusTimer: null,
    error: ''
  };
  window.leadEngineStore = store;

  function el(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text === undefined || text === null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function num(value, digits) {
    if (value === undefined || value === null || value === '') return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return n.toFixed(digits === undefined ? 2 : digits);
  }

  function signed(value, digits) {
    if (value === undefined || value === null) return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return (n > 0 ? '+' : '') + n.toFixed(digits === undefined ? 2 : digits);
  }

  function ageText(seconds) {
    if (seconds === undefined || seconds === null) return '—';
    if (seconds < 1) return Math.round(seconds * 1000) + 'ms';
    if (seconds < 90) return seconds.toFixed(1) + 's';
    return Math.round(seconds / 60) + 'm';
  }

  function row(key, value, cls) {
    return '<div class="le-row"><span class="k">' + esc(key) + '</span>' +
      '<span class="v ' + (cls || '') + '">' + value + '</span></div>';
  }

  function tone(value) {
    if (value === undefined || value === null) return 'dim';
    var n = Number(value);
    if (!isFinite(n) || n === 0) return 'dim';
    return n > 0 ? 'up' : 'down';
  }

  function meter(label, value, kind, max) {
    var pct = Math.max(0, Math.min(100, (Number(value) || 0) / (max || 100) * 100));
    return '<div class="le-meter ' + kind + '">' +
      '<div class="lbl"><span>' + esc(label) + '</span><span>' + num(value, 1) + '</span></div>' +
      '<div class="track"><span class="fill" style="width:' + pct.toFixed(1) + '%"></span></div>' +
      '</div>';
  }

  function chip(text, cls) {
    return '<span class="le-chip ' + (cls || '') + '">' + esc(text) + '</span>';
  }

  /* ---- fetching ---- */

  function get(path) {
    return fetch(path, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); });
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
      renderStatus();
    }).catch(function (e) { store.error = e.message || 'status failed'; });
  }

  function loadFrame() {
    if (!store.symbol) return Promise.resolve();
    return get('/api/lead-engine/state/' + encodeURIComponent(store.symbol))
      .then(function (data) {
        store.enabled = !!data.enabled;
        store.frame = data;
        store.error = '';
        render();
      })
      .catch(function (e) { store.error = e.message || 'state failed'; render(); });
  }

  /* ---- rendering ---- */

  function renderSymbolPicker() {
    var picker = el('leSymbol');
    if (!picker) return;
    var current = store.symbol;
    picker.innerHTML = store.symbols.map(function (s) {
      return '<option value="' + esc(s) + '"' + (s === current ? ' selected' : '') + '>' +
        esc(s) + '</option>';
    }).join('');
  }

  function renderStatus() {
    var target = el('leFleet');
    if (!target) return;
    var data = store.status;
    if (!data || !data.enabled) { target.innerHTML = ''; return; }
    var rows = (data.symbols || []).map(function (s) {
      return '<tr class="' + (s.symbol === store.symbol ? 'me' : '') + '">' +
        '<td>' + esc(s.symbol) + '</td>' +
        '<td>' + num(s.price, 4) + '</td>' +
        '<td class="' + (s.direction === 'long' ? 'up' : s.direction === 'short' ? 'down' : '') + '">' +
        esc(s.state) + '</td>' +
        '<td>' + num(s.long_pressure, 0) + '</td>' +
        '<td>' + num(s.short_pressure, 0) + '</td>' +
        '<td>' + esc(s.health) + '</td></tr>';
    }).join('');
    target.innerHTML =
      '<table class="le-table"><thead><tr><th>Symbol</th><th>Price</th><th>State</th>' +
      '<th>Long</th><th>Short</th><th>Feed</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function stateClass(state) {
    if (state === 'A_PLUS') return 'aplus';
    if (state === 'PRE_BREAK_LONG') return 'long';
    if (state === 'PRE_BREAK_SHORT') return 'short';
    if (state === 'HIGH_PROBABILITY') return 'aplus';
    if (state === 'REVERSAL_CANDIDATE') return 'rev';
    if (state === 'DATA_FAILURE' || state === 'INVALIDATED') return 'fail';
    if (state === 'WATCH' || state === 'PRE_SIGNAL') return 'watch';
    return 'idle';
  }

  function renderOff() {
    var body = el('leBody');
    if (!body) return;
    var detail = (store.status && store.status.detail) ||
      'The Market Lead Engine is switched off.';
    body.innerHTML = '<div class="le-off"><strong>Engine off.</strong><br>' + esc(detail) +
      '<br><br>While it is off this tab makes no repeated requests, no WebSocket is opened, ' +
      'no thread is started and no polling happens. Every other tab is unaffected either way.' +
      '</div>';
    var head = el('leHead');
    if (head) head.innerHTML = chip('ENGINE OFF', 'warn');
  }

  function renderHead(frame) {
    var head = el('leHead');
    if (!head) return;
    var health = frame.health || {};
    var stream = (store.status && store.status.stream) || {};
    var feedClass = health.status === 'OK' ? 'ok' : 'bad';
    head.innerHTML =
      '<span class="le-price">' + num(frame.price, 4) + '</span>' +
      chip(store.symbol, '') +
      chip('feed ' + (health.status || '—'), feedClass) +
      chip(stream.connected ? 'ws up' : 'ws down', stream.connected ? 'ok' : 'bad') +
      chip('latency ' + num(health.latency_ms, 0) + 'ms',
           (health.latency_ms || 0) > 2000 ? 'warn' : 'ok') +
      chip('book ' + ageText(health.last_book_age_s),
           (health.last_book_age_s || 0) > 5 ? 'warn' : 'ok') +
      chip('processing ' + num(health.processing_ms, 2) + 'ms', '') +
      chip(health.signals_enabled ? 'signals on' : 'signals off',
           health.signals_enabled ? 'ok' : 'bad');
  }

  function featureBars(features) {
    var names = Object.keys(features || {});
    if (!names.length) return '<div class="le-note">No features yet.</div>';
    return names.map(function (name) {
      var value = Math.max(0, Math.min(1, Number(features[name]) || 0));
      return '<div class="le-feature"><span>' + esc(name.replace(/_/g, ' ')) + '</span>' +
        '<span class="bar"><i style="width:' + (value * 100).toFixed(0) + '%"></i></span>' +
        '<span class="num">' + value.toFixed(2) + '</span></div>';
    }).join('');
  }

  function render() {
    var body = el('leBody');
    if (!body) return;
    if (store.enabled === false) { renderOff(); return; }
    var frame = store.frame;
    if (!frame) {
      body.innerHTML = '<div class="le-note">Waiting for the first frame…</div>';
      return;
    }
    if (frame.tracked === false) {
      body.innerHTML = '<div class="le-note">' + esc(store.symbol) +
        ' is not subscribed yet.</div>';
      return;
    }
    renderHead(frame);

    var book = frame.orderbook || {};
    var obi = book.obi || {};
    var micro = frame.microprice || {};
    var flow = frame.trade_flow || {};
    var windows = flow.windows || {};
    var cvd = frame.cvd || {};
    var liq = frame.liquidations || {};
    var liqW = liq.windows || {};
    var oi = frame.open_interest || {};
    var lead = frame.btc_lead || {};
    var smc = frame.smc || {};
    var ell = frame.elliott || {};
    var pressure = frame.pressure || {};
    var pre = frame.prebreak || {};
    var signal = frame.signal || {};

    var html = '';

    /* ACTIVE SIGNAL + PRESSURE */
    html += '<div class="le-grid">';
    html += '<div class="le-card"><h3>Active signal</h3>' +
      '<div style="margin:4px 0 8px;"><span class="le-state ' + stateClass(signal.state) + '">' +
      esc(signal.state || 'IDLE') + '</span></div>' +
      row('Direction', esc(signal.direction || '—')) +
      row('Confidence', num(signal.confidence, 1)) +
      row('Level', num(signal.level, 5)) +
      row('Break probability', num(signal.break_probability, 1)) +
      '<div class="le-note">' + esc(signal.reason || '') + '</div></div>';

    html += '<div class="le-card"><h3>Pressure</h3>' +
      meter('LONG_PRESSURE', pressure.long_pressure, 'long') +
      meter('SHORT_PRESSURE', pressure.short_pressure, 'short') +
      meter('BREAK_PROBABILITY long', (pre.long || {}).break_probability, 'break') +
      meter('BREAK_PROBABILITY short', (pre.short || {}).break_probability, 'break') +
      row('Conflict', num(pressure.conflict, 1)) +
      row('Net', signed(pressure.net, 1), tone(pressure.net)) +
      '<div class="le-note">' + esc(pressure.explanation || '') +
      ((pressure.missing || []).length
        ? '<br>Not yet contributing: ' + esc((pressure.missing || []).join(', ')) : '') +
      '</div></div>';

    /* ORDER BOOK */
    html += '<div class="le-card"><h3>Order book</h3>' +
      row('Best bid / ask', num(book.best_bid, 5) + ' / ' + num(book.best_ask, 5)) +
      row('Spread', num(book.spread, 5) + ' (' + num(book.spread_bps, 1) + ' bps)') +
      row('Microprice', num(book.microprice, 5)) +
      row('OBI 1', signed(obi.obi1, 3), tone(obi.obi1)) +
      row('OBI 5', signed(obi.obi5, 3), tone(obi.obi5)) +
      row('OBI 10', signed(obi.obi10, 3), tone(obi.obi10)) +
      row('OBI 25', signed(obi.obi25, 3), tone(obi.obi25)) +
      row('OBI 50', signed(obi.obi50, 3), tone(obi.obi50)) +
      row('Weighted OBI', signed(book.weighted_obi, 3), tone(book.weighted_obi)) +
      row('Bid pulling', num(book.bid_pulling, 1)) +
      row('Ask pulling', num(book.ask_pulling, 1)) +
      row('Bid replenishment', num(book.bid_replenishment, 1)) +
      row('Ask replenishment', num(book.ask_replenishment, 1)) +
      row('Absorption bid / ask', num(book.bid_absorption, 2) + ' / ' + num(book.ask_absorption, 2)) +
      row('Stacked bid / ask', (book.stacked_bid_levels || 0) + ' / ' + (book.stacked_ask_levels || 0)) +
      row('Walls bid / ask', (book.bid_walls || 0) + ' / ' + (book.ask_walls || 0)) +
      row('Wall persistence', ageText((book.max_wall_persistence_ms || 0) / 1000)) +
      row('Wall cancellations (60s)', book.wall_cancellations || 0) +
      '</div>';

    /* MICROPRICE */
    html += '<div class="le-card"><h3>Microprice</h3>' +
      row('Bias', esc(micro.microprice_bias || '—'),
          micro.microprice_bias === 'bullish' ? 'up'
            : micro.microprice_bias === 'bearish' ? 'down' : 'dim') +
      row('Offset from mid', signed(micro.microprice_offset_bps, 2) + ' bps',
          tone(micro.microprice_offset_bps)) +
      row('Δ 250ms', signed(micro.microprice_delta_250ms, 2), tone(micro.microprice_delta_250ms)) +
      row('Δ 1s', signed(micro.microprice_delta_1s, 2), tone(micro.microprice_delta_1s)) +
      row('Δ 3s', signed(micro.microprice_delta_3s, 2), tone(micro.microprice_delta_3s)) +
      row('Δ 5s', signed(micro.microprice_delta_5s, 2), tone(micro.microprice_delta_5s)) +
      '</div>';

    /* TRADE FLOW */
    var w5 = windows['5s'] || {};
    var w60 = windows['60s'] || {};
    var large = flow.large_trades || {};
    html += '<div class="le-card"><h3>Trade flow</h3>' +
      row('Taker buy (5s)', num(w5.buy_volume, 3)) +
      row('Taker sell (5s)', num(w5.sell_volume, 3)) +
      row('Delta (5s)', signed(w5.delta, 3), tone(w5.delta)) +
      row('Delta ratio (5s)', num(w5.delta_ratio, 2)) +
      row('Delta (60s)', signed(w60.delta, 3), tone(w60.delta)) +
      row('CVD', signed(cvd.cvd, 2), tone(cvd.cvd)) +
      row('Trades/sec', num(flow.trades_per_sec, 2)) +
      row('Volume/sec', num(flow.volume_per_sec, 3)) +
      row('Notional/sec', num(flow.notional_per_sec, 0)) +
      row('Acceleration', signed(flow.acceleration, 1), tone(flow.acceleration)) +
      row('Velocity z-score', num(flow.velocity_zscore, 2),
          (flow.velocity_zscore || 0) >= 2 ? 'up' : 'dim') +
      row('Velocity state', esc(flow.velocity_state || '—')) +
      row('Large trades (60s)', (large.buy_count || 0) + ' buy / ' + (large.sell_count || 0) + ' sell') +
      row('CVD vs price (15s)', esc((cvd.relationships || {})['15s'] || '—')) +
      row('Divergence', esc(cvd.divergence || 'none'), cvd.divergence ? 'down' : 'dim') +
      '</div>';

    /* LIQUIDATIONS */
    var l5 = liqW['5s'] || {}, l60 = liqW['60s'] || {};
    html += '<div class="le-card"><h3>Liquidations</h3>' +
      row('Long liquidations (60s)', num(l60.long_notional, 0)) +
      row('Short liquidations (60s)', num(l60.short_notional, 0)) +
      row('Count (60s)', l60.count || 0) +
      row('Velocity (5s)', num(l5.velocity, 0)) +
      row('Peak velocity (60s)', num(liq.peak_velocity_60s, 0)) +
      row('Acceleration', signed(liq.acceleration, 0), tone(liq.acceleration)) +
      row('State', esc(liq.state || '—'),
          liq.state === 'NEUTRAL' ? 'dim' : liq.state === 'SHORT_SQUEEZE' ? 'up' : 'down') +
      '</div>';

    /* OPEN INTEREST */
    html += '<div class="le-card"><h3>Open interest</h3>' +
      row('Current OI', num(oi.open_interest, 0)) +
      row('OI delta', signed(oi.oi_delta, 0), tone(oi.oi_delta)) +
      row('OI delta %', signed(oi.oi_delta_pct, 3), tone(oi.oi_delta_pct)) +
      row('Trend', esc(oi.oi_trend || '—')) +
      row('Interpretation', esc((oi.interpretation || '—').replace(/_/g, ' '))) +
      row('Age', ageText((frame.health || {}).oi_age_s)) +
      '</div>';

    /* BTC LEAD */
    html += '<div class="le-card"><h3>BTC lead</h3>' +
      row('BTC direction', esc(lead.btc_direction || '—'),
          lead.btc_direction === 'up' ? 'up' : lead.btc_direction === 'down' ? 'down' : 'dim') +
      row('BTC impulse', signed(lead.btc_impulse, 2), tone(lead.btc_impulse)) +
      row('Correlation', num(lead.correlation, 3)) +
      row('Estimated lag', lead.estimated_lag_ms === null || lead.estimated_lag_ms === undefined
        ? '—' : (lead.estimated_lag_ms / 1000).toFixed(0) + 's') +
      row('Lead score', signed(lead.btc_lead_score, 3), tone(lead.btc_lead_score)) +
      row('State', esc(lead.btc_lead_state || '—')) +
      '</div>';

    /* STRUCTURE */
    html += '<div class="le-card"><h3>Structure</h3>' +
      row('SMC trend', esc(smc.trend || '—'),
          smc.trend === 'bullish' ? 'up' : smc.trend === 'bearish' ? 'down' : 'dim') +
      row('Last swing', esc(smc.last_swing || '—')) +
      row('BOS', smc.bos ? esc(smc.bos_direction) : 'no', smc.bos ? 'up' : 'dim') +
      row('CHoCH', smc.choch ? esc(smc.choch_direction) : 'no', smc.choch ? 'down' : 'dim') +
      row('Sweep', esc(smc.sweep || 'none')) +
      row('FVGs', (smc.fair_value_gaps || []).length) +
      row('Order block', smc.order_block ? 'yes' : 'no') +
      row('Premium / discount', esc(smc.premium_discount || '—')) +
      row('Elliott candidate', esc(ell.current_wave_candidate || '—')) +
      row('Elliott phase', esc(ell.phase || '—')) +
      row('Elliott source', esc(ell.source || '—')) +
      '</div>';

    /* PRE-BREAK detail */
    ['short', 'long'].forEach(function (side) {
      var pb = pre[side] || {};
      html += '<div class="le-card"><h3>Pre-break ' + side + '</h3>' +
        row('Level', num(pb.level, 5)) +
        row('Tests', pb.tests || 0) +
        row('Probability', num(pb.break_probability, 1)) +
        (pb.note ? '<div class="le-note">' + esc(pb.note) + '</div>' : '') +
        featureBars(pb.features) + '</div>';
    });

    html += '</div>';   /* le-grid */
    body.innerHTML = html;
    renderStatus();
  }

  /* ---- lifecycle ---- */

  function tick() {
    if (!store.open) return;
    if (store.enabled === false) { renderOff(); return; }
    loadFrame();
  }

  store.open = false;

  store.show = function () {
    store.open = true;
    loadSymbols().then(function () {
      loadStatus();
      loadFrame();
    }).catch(function (e) {
      store.error = e.message || 'could not reach the engine';
      render();
    });
    if (store.timer) clearInterval(store.timer);
    if (store.statusTimer) clearInterval(store.statusTimer);
    store.timer = setInterval(tick, POLL_MS);
    store.statusTimer = setInterval(function () {
      if (store.open && store.enabled !== false) loadStatus();
    }, STATUS_POLL_MS);
  };

  /* Polling stops the moment the tab is hidden. The specification asks
     that an unopened tab cost nothing, and an interval that keeps firing
     against a hidden panel is exactly the cost it is asking about. */
  store.hide = function () {
    store.open = false;
    if (store.timer) { clearInterval(store.timer); store.timer = null; }
    if (store.statusTimer) { clearInterval(store.statusTimer); store.statusTimer = null; }
  };

  store.setSymbol = function (symbol) {
    store.symbol = symbol;
    store.frame = null;
    loadFrame();
  };

  document.addEventListener('DOMContentLoaded', function () {
    var picker = el('leSymbol');
    if (picker) {
      picker.addEventListener('change', function () { store.setSymbol(picker.value); });
    }
    var refresh = el('leRefresh');
    if (refresh) {
      refresh.addEventListener('click', function () { loadStatus(); loadFrame(); });
    }
  });
})();
