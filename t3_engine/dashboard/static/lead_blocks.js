/* The Lead Engine's panel content: built once, values bound by id.
 *
 * Every block below returns a string of HTML built ONE TIME by
 * `buildBlocks`, and every changing number inside it is a registered
 * cell. `applyFrame` then writes only the cells whose text actually
 * changed. Labels ("LONG_PRESSURE", "CVD", "OBI") are part of the built
 * markup and are never rewritten — which is the requirement that static
 * labels render once and only values update.
 *
 * Shared by the tab (#tab-lead) and the Market Workspace, so the two
 * cannot drift apart.
 */
(function (global) {
  'use strict';

  var LP = global.LeadPanel;
  var esc = LP.esc, fmt = LP.fmt, signed = LP.signed, ageText = LP.ageText;

  var LAYERS = ['flow', 'book', 'structure', 'derivatives', 'btc_lead'];

  function card(title, inner) {
    return '<div class="le-card"><h3>' + esc(title) + '</h3>' + inner + '</div>';
  }

  function meter(panel, id, label) {
    return '<div class="le-meter ' + (label.indexOf('SHORT') === 0 ? 'short' :
      label.indexOf('LONG') === 0 ? 'long' : 'break') + '">' +
      '<div class="lbl"><span>' + esc(label) + '</span>' +
      panel.cell(id + ':text') + '</div>' +
      '<div class="track"><span class="fill" data-le="' + id + ':bar"></span></div></div>';
  }

  function layerRow(panel, name) {
    return '<div class="le-layer">' +
      '<span class="name">' + esc(name.replace('_', ' ')) + '</span>' +
      '<span class="track"><i class="long" data-le="L:' + name + ':long"></i>' +
      '<i class="short" data-le="L:' + name + ':short"></i></span>' +
      panel.cell('L:' + name + ':score') +
      '<span class="conf le-num" data-le="L:' + name + ':conf">—</span></div>';
  }

  function clocks(panel) {
    var names = [['ws', 'WS LATENCY'], ['book', 'BOOK AGE'], ['trade', 'TRADE AGE'],
                 ['ticker', 'TICKER AGE'], ['oi', 'OI AGE'],
                 ['proc', 'PROCESSING'], ['ui', 'UI']];
    return '<div class="le-clocks">' + names.map(function (pair) {
      return '<span class="le-clock" data-le="clock:' + pair[0] + ':box">' +
        esc(pair[1]) + ' <b data-le="clock:' + pair[0] + '">—</b></span>';
    }).join('') + '</div>';
  }

  function buildBlocks(panel) {
    var p = panel;
    var html = '';

    html += '<div class="le-grid">';

    html += card('Active signal',
      '<div style="margin:4px 0 8px;"><span class="le-state" data-le="sig:state">IDLE</span>' +
      ' <span class="le-badge-score" data-le="sig:kind">MODEL SCORE</span></div>' +
      p.row('sig:direction', 'Direction') +
      p.row('sig:level', 'Level') +
      p.row('sig:score', 'Break score') +
      p.row('sig:prob', 'Calibrated probability') +
      '<div class="le-note" data-le="sig:reason"></div>' +
      '<div class="le-note" data-le="sig:calnote"></div>');

    html += card('Pressure',
      meter(p, 'pr:long', 'LONG_PRESSURE') +
      meter(p, 'pr:short', 'SHORT_PRESSURE') +
      meter(p, 'pb:long', 'BREAK_SCORE long') +
      meter(p, 'pb:short', 'BREAK_SCORE short') +
      p.row('pr:net', 'Net') +
      p.row('pr:conf', 'Confidence') +
      '<div class="le-note" data-le="pr:explain"></div>' +
      '<div class="le-conflict low" data-le="pr:conflict">—</div>');

    html += card('Layers',
      LAYERS.map(function (name) { return layerRow(p, name); }).join('') +
      '<div class="le-note" data-le="L:missing"></div>');

    html += card('Order book',
      p.row('ob:bidask', 'Best bid / ask') +
      p.row('ob:spread', 'Spread') +
      p.row('ob:micro', 'Microprice') +
      p.row('ob:obi1', 'OBI 1') +
      p.row('ob:obi5', 'OBI 5') +
      p.row('ob:obi10', 'OBI 10') +
      p.row('ob:obi25', 'OBI 25') +
      p.row('ob:obi50', 'OBI 50') +
      p.row('ob:wobi', 'Weighted OBI') +
      p.row('ob:top', 'Top book score') +
      p.row('ob:deep', 'Deep book score') +
      p.row('ob:consistency', 'Book consistency') +
      p.row('ob:alignment', 'BOOK_ALIGNMENT') +
      '<div class="le-note" data-le="ob:label"></div>' +
      p.row('ob:bidpull', 'Bid pulling') +
      p.row('ob:askpull', 'Ask pulling') +
      p.row('ob:bidrep', 'Bid replenishment') +
      p.row('ob:askrep', 'Ask replenishment') +
      p.row('ob:absbid', 'Bid absorption score') +
      p.row('ob:absask', 'Ask absorption score') +
      p.row('ob:walls', 'Walls (persistent/total)') +
      p.row('ob:wallbias', 'Wall bias') +
      p.row('ob:spoof', 'Spoofs (60s)'));

    html += card('Microprice',
      p.row('mp:bias', 'Bias') +
      p.row('mp:offset', 'Offset from mid (bps)') +
      p.row('mp:d250', 'Δ 250ms') +
      p.row('mp:d1', 'Δ 1s') +
      p.row('mp:d3', 'Δ 3s') +
      p.row('mp:d5', 'Δ 5s'));

    html += card('Trade flow',
      p.row('fl:nd5', 'Normalised delta 5s') +
      p.row('fl:nd60', 'Normalised delta 60s') +
      p.row('fl:buy', 'Taker buy (5s)') +
      p.row('fl:sell', 'Taker sell (5s)') +
      p.row('fl:delta', 'Delta (5s)') +
      p.row('fl:ratio', 'Raw ratio (diagnostic)') +
      p.row('fl:cvd', 'CVD') +
      p.row('fl:tps', 'Trades/sec') +
      p.row('fl:vps', 'Volume/sec') +
      p.row('fl:accel', 'Acceleration') +
      p.row('fl:z', 'Velocity z-score') +
      p.row('fl:vstate', 'Velocity state') +
      p.row('fl:large', 'Large trades (60s)') +
      p.row('fl:div', 'CVD divergence'));

    html += card('Derivatives',
      p.row('dv:oi', 'Open interest') +
      p.row('dv:oidelta', 'OI delta') +
      p.row('dv:oipct', 'OI delta %') +
      p.row('dv:oitrend', 'OI trend') +
      p.row('dv:interp', 'Interpretation') +
      p.row('dv:funding', 'Funding rate') +
      p.row('dv:liqlong', 'Long liquidations (60s)') +
      p.row('dv:liqshort', 'Short liquidations (60s)') +
      p.row('dv:liqvel', 'Liquidation velocity') +
      p.row('dv:liqstate', 'Liquidation state'));

    html += card('BTC lead',
      p.row('bt:dir', 'BTC direction') +
      p.row('bt:impulse', 'BTC impulse') +
      p.row('bt:corr', 'Correlation') +
      p.row('bt:lag', 'Estimated lag') +
      p.row('bt:score', 'Lead score') +
      p.row('bt:state', 'State'));

    html += card('Structure',
      p.row('st:trend', 'SMC trend') +
      p.row('st:swing', 'Last swing') +
      p.row('st:bos', 'BOS') +
      p.row('st:choch', 'CHoCH') +
      p.row('st:sweep', 'Sweep') +
      p.row('st:fvg', 'FVGs') +
      p.row('st:ob', 'Order block') +
      p.row('st:pd', 'Premium / discount') +
      p.row('st:ell', 'Elliott candidate') +
      p.row('st:ellphase', 'Elliott phase') +
      p.row('st:sup', 'Nearest support') +
      p.row('st:res', 'Nearest resistance'));

    ['short', 'long'].forEach(function (side) {
      var rows = ['compression', 'fading_bounces', 'depth_drain', 'defender_pulling',
                  'attacker_stacking', 'flow_pressure', 'microprice_lean',
                  'velocity_rising', 'btc_alignment', 'repeated_tests']
        .map(function (name) {
          return '<div class="le-feature"><span>' + esc(name.replace(/_/g, ' ')) + '</span>' +
            '<span class="bar"><i data-le="F:' + side + ':' + name + ':bar"></i></span>' +
            '<span class="num le-num" data-le="F:' + side + ':' + name + '">—</span></div>';
        }).join('');
      html += card('Pre-break ' + side,
        p.row('F:' + side + ':level', 'Level') +
        p.row('F:' + side + ':tests', 'Tests') +
        p.row('F:' + side + ':score', 'Break score') +
        '<div class="le-note" data-le="F:' + side + ':note"></div>' + rows);
    });

    html += card('Feed health',
      clocks(p) +
      p.row('h:status', 'Engine status') +
      p.row('h:signals', 'Signals') +
      p.row('h:ws', 'WS connected') +
      p.row('h:synced', 'Book synced') +
      p.row('h:dropped', 'Dropped messages') +
      p.row('h:reconnects', 'Reconnects') +
      '<div class="le-note" data-le="h:reasons"></div>');

    html += '</div>';
    return html;
  }

  /* ---- applying a frame --------------------------------------------- */

  function applyFrame(panel, frame, extra) {
    if (!frame || !panel.mounted) return;
    var p = panel;
    var book = frame.orderbook || {};
    var obi = book.obi || {};
    var align = book.alignment || {};
    var micro = frame.microprice || {};
    var flow = frame.trade_flow || {};
    var w5 = (flow.windows || {})['5s'] || {};
    var cvd = frame.cvd || {};
    var liq = frame.liquidations || {};
    var liqW = liq.windows || {};
    var oi = frame.open_interest || {};
    var lead = frame.btc_lead || {};
    var smc = frame.smc || {};
    var ell = frame.elliott || {};
    var pressure = frame.pressure || {};
    var layers = frame.layers || {};
    var pre = frame.prebreak || {};
    var sig = frame.signal || {};
    var health = frame.health || {};

    /* signal */
    p.set('sig:state', sig.state || 'IDLE');
    p.klass('sig:state', 'le-state ' + stateClass(sig.state));
    p.set('sig:direction', sig.direction || '—');
    p.set('sig:level', fmt(sig.level, 5));
    p.set('sig:score', fmt(sig.break_probability, 1));
    var cal = ((pre[sig.direction === 'long' ? 'long' : 'short'] || {}).calibration) || {};
    p.set('sig:kind', cal.kind === 'PROBABILITY' ? 'PROBABILITY' : 'MODEL SCORE');
    p.set('sig:prob', cal.probability === null || cal.probability === undefined
      ? 'not calibrated' : fmt(cal.probability, 1) + '%');
    p.set('sig:reason', sig.reason || '');
    p.set('sig:calnote', cal.note || '');

    /* pressure */
    p.set('pr:long:text', fmt(pressure.long_pressure, 1));
    p.width('pr:long:bar', pressure.long_pressure);
    p.set('pr:short:text', fmt(pressure.short_pressure, 1));
    p.width('pr:short:bar', pressure.short_pressure);
    p.set('pr:net', signed(pressure.net, 1));
    p.colour('pr:net', pressure.net, 'pressure');
    p.set('pr:conf', fmt(pressure.confidence, 2));
    p.set('pr:explain', pressure.explanation || '');
    var conflict = pressure.conflict_detail || {};
    var level = String(conflict.level || 'CONFLICT_LOW');
    p.set('pr:conflict', level + (conflict.note ? ' — ' + conflict.note : ''));
    p.klass('pr:conflict', 'le-conflict ' + level.replace('CONFLICT_', '').toLowerCase());

    /* layers */
    LAYERS.forEach(function (name) {
      var layer = layers[name] || {};
      p.set('L:' + name + ':score', signed(layer.score, 2));
      p.colour('L:' + name + ':score', layer.score, 'ratio');
      p.set('L:' + name + ':conf', fmt(layer.confidence, 2));
      var half = Math.abs(Number(layer.score) || 0) * 50;
      p.width('L:' + name + ':long', (Number(layer.score) || 0) > 0 ? half : 0);
      p.width('L:' + name + ':short', (Number(layer.score) || 0) < 0 ? half : 0);
    });
    p.set('L:missing', (pressure.missing || []).length
      ? 'Not contributing: ' + (pressure.missing || []).join(', ') : '');

    /* order book */
    p.set('ob:bidask', fmt(book.best_bid, 5) + ' / ' + fmt(book.best_ask, 5));
    p.set('ob:spread', fmt(book.spread, 5) + ' (' + fmt(book.spread_bps, 1) + ' bps)');
    p.set('ob:micro', fmt(book.microprice, 5));
    [1, 5, 10, 25, 50].forEach(function (depth) {
      var id = 'ob:obi' + depth;
      p.set(id, signed(obi['obi' + depth], 3));
      p.colour(id, obi['obi' + depth], 'ratio');
    });
    p.set('ob:wobi', signed(book.weighted_obi, 3));
    p.colour('ob:wobi', book.weighted_obi, 'ratio');
    p.set('ob:top', signed(align.top_book_score, 3));
    p.colour('ob:top', align.top_book_score, 'ratio');
    p.set('ob:deep', signed(align.deep_book_score, 3));
    p.colour('ob:deep', align.deep_book_score, 'ratio');
    p.set('ob:consistency', fmt(align.consistency, 3));
    p.set('ob:alignment', signed(align.book_alignment, 3));
    p.colour('ob:alignment', align.book_alignment, 'ratio');
    p.set('ob:label', book.alignment_label || '');
    p.set('ob:bidpull', fmt(book.bid_pulling, 1));
    p.set('ob:askpull', fmt(book.ask_pulling, 1));
    p.set('ob:bidrep', fmt(book.bid_replenishment, 1));
    p.set('ob:askrep', fmt(book.ask_replenishment, 1));
    var bookLayer = (layers.book || {}).detail || {};
    p.set('ob:absbid', fmt(bookLayer.bid_absorption_score, 2));
    p.set('ob:absask', fmt(bookLayer.ask_absorption_score, 2));
    var walls = frame.walls || {};
    var byKind = walls.by_classification || {};
    p.set('ob:walls', (byKind.PERSISTENT_WALL || 0) + ' / ' + (walls.walls || []).length);
    p.set('ob:wallbias', signed(walls.wall_bias, 3));
    p.colour('ob:wallbias', walls.wall_bias, 'ratio');
    p.set('ob:spoof', walls.spoofs_60s === undefined ? '—' : walls.spoofs_60s);

    /* microprice */
    p.set('mp:bias', micro.microprice_bias || '—');
    p.set('mp:offset', signed(micro.microprice_offset_bps, 2));
    p.colour('mp:offset', micro.microprice_offset_bps, 'bps');
    p.set('mp:d250', signed(micro.microprice_delta_250ms, 2));
    p.set('mp:d1', signed(micro.microprice_delta_1s, 2));
    p.set('mp:d3', signed(micro.microprice_delta_3s, 2));
    p.set('mp:d5', signed(micro.microprice_delta_5s, 2));

    /* flow */
    var flowDetail = (layers.flow || {}).detail || {};
    p.set('fl:nd5', signed(flowDetail.normalized_delta_5s, 3));
    p.colour('fl:nd5', flowDetail.normalized_delta_5s, 'ratio');
    p.set('fl:nd60', signed(flowDetail.normalized_delta_60s, 3));
    p.colour('fl:nd60', flowDetail.normalized_delta_60s, 'ratio');
    p.set('fl:buy', fmt(w5.buy_volume, 3));
    p.set('fl:sell', fmt(w5.sell_volume, 3));
    p.set('fl:delta', signed(w5.delta, 3));
    p.colour('fl:delta', w5.delta, 'generic');
    p.set('fl:ratio', fmt(w5.delta_ratio, 3));
    p.set('fl:cvd', signed(cvd.cvd, 2));
    p.set('fl:tps', fmt(flow.trades_per_sec, 2));
    p.set('fl:vps', fmt(flow.volume_per_sec, 3));
    p.set('fl:accel', signed(flow.acceleration, 1));
    p.set('fl:z', fmt(flow.velocity_zscore, 2));
    p.set('fl:vstate', flow.velocity_state || '—');
    var large = flow.large_trades || {};
    p.set('fl:large', (large.buy_count || 0) + ' buy / ' + (large.sell_count || 0) + ' sell');
    p.set('fl:div', cvd.divergence || 'none');

    /* derivatives */
    p.set('dv:oi', fmt(oi.open_interest, 0));
    p.set('dv:oidelta', signed(oi.oi_delta, 0));
    p.set('dv:oipct', signed(oi.oi_delta_pct, 3));
    p.set('dv:oitrend', oi.oi_trend || '—');
    p.set('dv:interp', String(oi.interpretation || '—').replace(/_/g, ' '));
    var derivDetail = (layers.derivatives || {}).detail || {};
    p.set('dv:funding', derivDetail.funding_rate === undefined
      ? '—' : signed(derivDetail.funding_rate * 100, 4) + '%');
    var l60 = liqW['60s'] || {};
    p.set('dv:liqlong', fmt(l60.long_notional, 0));
    p.set('dv:liqshort', fmt(l60.short_notional, 0));
    p.set('dv:liqvel', fmt(liq.velocity, 0));
    p.set('dv:liqstate', liq.state || '—');

    /* btc */
    p.set('bt:dir', lead.btc_direction || '—');
    p.set('bt:impulse', signed(lead.btc_impulse, 2));
    p.colour('bt:impulse', lead.btc_impulse, 'ratio');
    p.set('bt:corr', fmt(lead.correlation, 3));
    p.set('bt:lag', lead.estimated_lag_ms === null || lead.estimated_lag_ms === undefined
      ? '—' : (lead.estimated_lag_ms / 1000).toFixed(0) + 's');
    p.set('bt:score', signed(lead.btc_lead_score, 3));
    p.colour('bt:score', lead.btc_lead_score, 'ratio');
    p.set('bt:state', lead.btc_lead_state || '—');

    /* structure */
    p.set('st:trend', smc.trend || '—');
    p.set('st:swing', smc.last_swing || '—');
    p.set('st:bos', smc.bos ? String(smc.bos_direction) : 'no');
    p.set('st:choch', smc.choch ? String(smc.choch_direction) : 'no');
    p.set('st:sweep', smc.sweep || 'none');
    p.set('st:fvg', (smc.fair_value_gaps || []).length);
    p.set('st:ob', smc.order_block ? 'yes' : 'no');
    p.set('st:pd', smc.premium_discount || '—');
    p.set('st:ell', ell.current_wave_candidate || '—');
    p.set('st:ellphase', ell.phase || '—');
    p.set('st:sup', fmt((pre.short || {}).level, 5));
    p.set('st:res', fmt((pre.long || {}).level, 5));

    /* pre-break */
    ['short', 'long'].forEach(function (side) {
      var pb = pre[side] || {};
      var features = pb.features || {};
      p.set('F:' + side + ':level', fmt(pb.level, 5));
      p.set('F:' + side + ':tests', pb.tests === undefined ? '—' : pb.tests);
      p.set('F:' + side + ':score', fmt(pb.break_score, 1));
      p.set('F:' + side + ':note', pb.note || '');
      Object.keys(features).forEach(function (name) {
        var value = Math.max(0, Math.min(1, Number(features[name]) || 0));
        p.set('F:' + side + ':' + name, value.toFixed(2));
        p.width('F:' + side + ':' + name + ':bar', value * 100);
      });
    });

    /* health — the four clocks, each named for what it measures */
    var clockValues = {
      ws: health.ws_latency_ms, book: health.book_age_ms, trade: health.trade_age_ms,
      ticker: health.ticker_age_ms, oi: health.oi_age_ms, proc: health.processing_ms,
      ui: (extra && extra.uiLatencyMs)
    };
    var clockLimits = { ws: 2000, book: 5000, trade: 30000, ticker: 30000,
                        oi: 900000, proc: 50, ui: 1000 };
    Object.keys(clockValues).forEach(function (key) {
      var value = clockValues[key];
      p.set('clock:' + key, key === 'proc' ? fmt(value, 2) + 'ms' : ageText(value));
      var limit = clockLimits[key];
      var cls = 'le-clock';
      if (value !== null && value !== undefined && isFinite(Number(value))) {
        if (Number(value) > limit) cls += ' bad';
        else if (Number(value) > limit * 0.5) cls += ' warn';
      }
      p.klass('clock:' + key + ':box', cls);
    });
    p.set('h:status', health.status || '—');
    p.colour('h:status', health.status === 'OK' ? 1 : -1, 'ratio');
    p.set('h:signals', health.signals_enabled ? 'ENABLED' : 'OFF');
    p.colour('h:signals', health.signals_enabled ? 1 : -1, 'ratio');
    p.set('h:ws', health.ws_connected ? 'yes' : 'no');
    p.set('h:synced', health.orderbook_synced ? 'yes' : 'no');
    p.set('h:dropped', health.dropped_messages || 0);
    p.set('h:reconnects', health.reconnects || 0);
    p.set('h:reasons', (health.reasons || []).join('; '));
  }

  function stateClass(state) {
    if (state === 'A_PLUS' || state === 'HIGH_PROBABILITY') return 'aplus';
    if (state === 'PRE_BREAK_LONG') return 'long';
    if (state === 'PRE_BREAK_SHORT') return 'short';
    if (state === 'REVERSAL_CANDIDATE') return 'rev';
    if (state === 'DATA_FAILURE' || state === 'INVALIDATED') return 'fail';
    if (state === 'WATCH' || state === 'PRE_SIGNAL') return 'watch';
    return 'idle';
  }

  global.LeadBlocks = { buildBlocks: buildBlocks, applyFrame: applyFrame,
                        stateClass: stateClass, LAYERS: LAYERS };
})(window);
