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

  /* ---- labels -------------------------------------------------------

     A HAND-WRITTEN dictionary, and the page is marked `translate="no"`
     so a browser never runs its own over the top.

     The reason is not tidiness. Machine translation of a trading panel
     produces confident nonsense: "Order book" becomes a book you read,
     "Bid pulling" becomes something being dragged, "Sweep" becomes
     sweeping the floor. Acronyms fare worse - OBI, CVD, BOS, CHoCH, FVG
     and Microprice are names, not words, and a translator that renders
     them has changed what the panel says.

     So every label is written once, here, by hand. Terms that ARE names
     stay in the Latin alphabet on purpose. */

  var LABELS = {
      "Order book": "Стакан",
      "Wall persistence": "Время жизни стенки",
      "Wall cancellations": "Снятые стенки",
      "Bid pulling": "Снятие bid-ликвидности",
      "Ask pulling": "Снятие ask-ликвидности",
      "Bid replenishment": "Пополнение bid",
      "Ask replenishment": "Пополнение ask",
      "CVD": "CVD",
      "Open interest": "Открытый интерес",
      "Microprice": "Microprice",
      "Active signal": "Активный сигнал",
      "Direction": "Направление",
      "Level": "Уровень",
      "Break score": "Break score",
      "Calibrated probability": "Калиброванная вероятность",
      "Pressure": "Давление",
      "Net": "Нетто",
      "Confidence": "Confidence",
      "Layers": "Слои",
      "Best bid / ask": "Лучшие bid / ask",
      "Spread": "Спред",
      "OBI 1": "OBI 1",
      "OBI 5": "OBI 5",
      "OBI 10": "OBI 10",
      "OBI 25": "OBI 25",
      "OBI 50": "OBI 50",
      "Weighted OBI": "Взвешенный OBI",
      "Top book score": "Top book score",
      "Deep book score": "Deep book score",
      "Book consistency": "Согласованность стакана",
      "BOOK_ALIGNMENT": "BOOK_ALIGNMENT",
      "Bid absorption score": "Поглощение на bid",
      "Ask absorption score": "Поглощение на ask",
      "Walls (persistent/total)": "Стенки (устойчивые/всего)",
      "Wall bias": "Перевес стенок",
      "Spoofs (60s)": "Спуфинг (60с)",
      "Bias": "Смещение",
      "Offset from mid (bps)": "Отклонение от mid (bps)",
      "Normalised delta 5s": "Нормализованная дельта 5с",
      "Normalised delta 60s": "Нормализованная дельта 60с",
      "Taker buy (5s)": "Тейкер-покупки (5с)",
      "Taker sell (5s)": "Тейкер-продажи (5с)",
      "Delta (5s)": "Дельта (5с)",
      "Raw ratio (diagnostic)": "Сырое отношение (диагностика)",
      "Trade flow": "Поток сделок",
      "Trades/sec": "Сделок/с",
      "Volume/sec": "Объём/с",
      "Acceleration": "Ускорение",
      "Velocity z-score": "Velocity z-score",
      "Velocity state": "Состояние скорости",
      "Large trades (60s)": "Крупные сделки (60с)",
      "CVD divergence": "Дивергенция CVD",
      "Derivatives": "Деривативы",
      "OI delta": "OI delta",
      "OI delta %": "OI delta %",
      "OI trend": "Тренд OI",
      "Interpretation": "Интерпретация",
      "Funding rate": "Funding rate",
      "Long liquidations (60s)": "Ликвидации лонгов (60с)",
      "Short liquidations (60s)": "Ликвидации шортов (60с)",
      "Liquidation velocity": "Скорость ликвидаций",
      "Liquidation state": "Состояние ликвидаций",
      "BTC lead": "Лидерство BTC",
      "BTC direction": "Направление BTC",
      "BTC impulse": "Импульс BTC",
      "Correlation": "Корреляция",
      "Estimated lag": "Оценка лага",
      "Lead score": "Lead score",
      "State": "Состояние",
      "Structure": "Структура",
      "SMC trend": "Тренд SMC",
      "Last swing": "Последний swing",
      "BOS": "BOS",
      "CHoCH": "CHoCH",
      "Sweep": "Sweep",
      "FVGs": "FVG",
      "Order block": "Order block",
      "Premium / discount": "Premium / discount",
      "Elliott candidate": "Кандидат волны Эллиотта",
      "Elliott phase": "Фаза Эллиотта",
      "Nearest support": "Ближайшая поддержка",
      "Nearest resistance": "Ближайшее сопротивление",
      "Tests": "Тестов",
      "Feed health": "Состояние данных",
      "Pre-break long": "Pre-break лонг",
      "Pre-break short": "Pre-break шорт",
      "Engine status": "Статус движка",
      "Signals": "Сигналы",
      "WS connected": "WS подключён",
      "Book synced": "Стакан синхронизирован",
      "Dropped messages": "Потеряно сообщений",
      "Reconnects": "Переподключений",

      // The virtual ledger. "Paper" stays in the note, not in a label:
      // the card's own subtitle says it in Russian once, plainly.
      "Virtual trades": "Виртуальные сделки",
      "Open positions": "Открытых позиций",
      "Waiting for a fill": "Ждут исполнения",
      "Closed trades": "Закрытых сделок",
      "Win rate": "Доля прибыльных",
      "Gross P&L": "P&L до издержек",
      "Fees paid": "Комиссии",
      "Net P&L": "Чистый P&L",
      "Open P&L": "P&L по открытым",
      "Costs vs gross": "Издержки к прибыли",
      "Not filled - stream gap": "Не исполнено - разрыв потока",
      "Uncertain exits": "Неточные выходы",
      "Skipped - stale book": "Пропущено - устаревший стакан",
      "Journal": "Журнал"
  };

  function label(text) {
    // An untranslated label is a bug to notice, not one to hide, so the
    // English falls through visibly rather than silently.
    return Object.prototype.hasOwnProperty.call(LABELS, text) ? LABELS[text] : text;
  }

  /* ---- the virtual journal ------------------------------------------ */

  // Status and exit reason are shown in Russian, but the words are
  // chosen here by hand rather than translated: "ABANDONED" is a state
  // of this ledger, not an English word to be rendered.
  var TRADE_STATUS = {
    PENDING: 'ждёт стакан', OPEN: 'открыта', CLOSED: 'закрыта',
    ABANDONED: 'не исполнена'
  };
  var EXIT_REASON = { stop: 'стоп', target: 'цель', time: 'по времени' };

  function clockText(ms) {
    if (!ms) return '—';
    var d = new Date(Number(ms));
    if (isNaN(d.getTime())) return '—';
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) +
      ':' + ('0' + d.getSeconds()).slice(-2);
  }

  function priceText(value) {
    if (value === null || value === undefined) return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    // Price precision is the instrument's, not a constant: 0.02 on an
    // altcoin and 0.02 on BTC are not the same number of digits.
    return n.toFixed(n >= 1000 ? 1 : n >= 10 ? 3 : 5);
  }

  function journalRows(rows) {
    if (!rows || !rows.length) {
      return '<div class="le-note">Пока ни одного сигнала не отработано.</div>';
    }
    return rows.map(function (row) {
      var net = row.net_pnl;
      var tone = row.status !== 'CLOSED' ? 'dim' : net > 0 ? 'up' : net < 0 ? 'down' : 'dim';
      var side = row.direction === 'long' ? 'LONG' : 'SHORT';
      var cells =
        '<span class="j-time">' + esc(clockText(row.signal_at_ms)) + '</span>' +
        '<span class="j-side ' + (row.direction === 'long' ? 'long' : 'short') + '">' +
          esc(side) + '</span>' +
        '<span class="j-state">' + esc(row.signal_state || '') + '</span>' +
        '<span class="j-px le-num">' + esc(priceText(row.entry_price)) + '</span>' +
        '<span class="j-px le-num">' + esc(priceText(row.exit_price)) + '</span>' +
        '<span class="j-why">' +
          esc(row.status === 'CLOSED'
              ? (EXIT_REASON[row.exit_reason] || row.exit_reason || '')
              : (TRADE_STATUS[row.status] || row.status)) +
          (row.gap_uncertain ? ' <b title="выход оценён через разрыв потока">~</b>' : '') +
        '</span>' +
        '<span class="j-net le-num ' + tone + '">' +
          (row.status === 'CLOSED' ? signed(net, 3) : '—') + '</span>' +
        '<span class="j-fee le-num">' +
          (row.fees ? '-' + fmt(row.fees, 3) : '—') + '</span>';
      return '<div class="le-jrow' + (row.status === 'ABANDONED' ? ' skipped' : '') +
        '">' + cells + '</div>';
    }).join('');
  }

  function card(title, inner) {
    return '<div class="le-card"><h3>' + esc(label(title)) + '</h3>' + inner + '</div>';
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
      p.row('sig:direction', label('Direction')) +
      p.row('sig:level', label('Level')) +
      p.row('sig:score', label('Break score')) +
      p.row('sig:prob', label('Calibrated probability')) +
      '<div class="le-note" data-le="sig:reason"></div>' +
      '<div class="le-note" data-le="sig:calnote"></div>');

    html += card('Pressure',
      meter(p, 'pr:long', 'LONG_PRESSURE') +
      meter(p, 'pr:short', 'SHORT_PRESSURE') +
      meter(p, 'pb:long', 'BREAK_SCORE long') +
      meter(p, 'pb:short', 'BREAK_SCORE short') +
      p.row('pr:net', label('Net')) +
      p.row('pr:conf', label('Confidence')) +
      '<div class="le-note" data-le="pr:explain"></div>' +
      '<div class="le-conflict low" data-le="pr:conflict">—</div>');

    html += card('Layers',
      LAYERS.map(function (name) { return layerRow(p, name); }).join('') +
      '<div class="le-note" data-le="L:missing"></div>');

    html += card('Order book',
      p.row('ob:bidask', label('Best bid / ask')) +
      p.row('ob:spread', label('Spread')) +
      p.row('ob:micro', label('Microprice')) +
      p.row('ob:obi1', label('OBI 1')) +
      p.row('ob:obi5', label('OBI 5')) +
      p.row('ob:obi10', label('OBI 10')) +
      p.row('ob:obi25', label('OBI 25')) +
      p.row('ob:obi50', label('OBI 50')) +
      p.row('ob:wobi', label('Weighted OBI')) +
      p.row('ob:top', label('Top book score')) +
      p.row('ob:deep', label('Deep book score')) +
      p.row('ob:consistency', label('Book consistency')) +
      p.row('ob:alignment', label('BOOK_ALIGNMENT')) +
      '<div class="le-note" data-le="ob:label"></div>' +
      p.row('ob:bidpull', label('Bid pulling')) +
      p.row('ob:askpull', label('Ask pulling')) +
      p.row('ob:bidrep', label('Bid replenishment')) +
      p.row('ob:askrep', label('Ask replenishment')) +
      p.row('ob:absbid', label('Bid absorption score')) +
      p.row('ob:absask', label('Ask absorption score')) +
      p.row('ob:walls', label('Walls (persistent/total)')) +
      p.row('ob:wallbias', label('Wall bias')) +
      p.row('ob:spoof', label('Spoofs (60s)')));

    html += card('Microprice',
      p.row('mp:bias', label('Bias')) +
      p.row('mp:offset', label('Offset from mid (bps)')) +
      p.row('mp:d250', label('Δ 250ms')) +
      p.row('mp:d1', label('Δ 1s')) +
      p.row('mp:d3', label('Δ 3s')) +
      p.row('mp:d5', label('Δ 5s')));

    html += card('Trade flow',
      p.row('fl:nd5', label('Normalised delta 5s')) +
      p.row('fl:nd60', label('Normalised delta 60s')) +
      p.row('fl:buy', label('Taker buy (5s)')) +
      p.row('fl:sell', label('Taker sell (5s)')) +
      p.row('fl:delta', label('Delta (5s)')) +
      p.row('fl:ratio', label('Raw ratio (diagnostic)')) +
      p.row('fl:cvd', label('CVD')) +
      p.row('fl:tps', label('Trades/sec')) +
      p.row('fl:vps', label('Volume/sec')) +
      p.row('fl:accel', label('Acceleration')) +
      p.row('fl:z', label('Velocity z-score')) +
      p.row('fl:vstate', label('Velocity state')) +
      p.row('fl:large', label('Large trades (60s)')) +
      p.row('fl:div', label('CVD divergence')));

    html += card('Derivatives',
      p.row('dv:oi', label('Open interest')) +
      p.row('dv:oidelta', label('OI delta')) +
      p.row('dv:oipct', label('OI delta %')) +
      p.row('dv:oitrend', label('OI trend')) +
      p.row('dv:interp', label('Interpretation')) +
      p.row('dv:funding', label('Funding rate')) +
      p.row('dv:liqlong', label('Long liquidations (60s)')) +
      p.row('dv:liqshort', label('Short liquidations (60s)')) +
      p.row('dv:liqvel', label('Liquidation velocity')) +
      p.row('dv:liqstate', label('Liquidation state')));

    html += card('BTC lead',
      p.row('bt:dir', label('BTC direction')) +
      p.row('bt:impulse', label('BTC impulse')) +
      p.row('bt:corr', label('Correlation')) +
      p.row('bt:lag', label('Estimated lag')) +
      p.row('bt:score', label('Lead score')) +
      p.row('bt:state', label('State')));

    html += card('Structure',
      p.row('st:trend', label('SMC trend')) +
      p.row('st:swing', label('Last swing')) +
      p.row('st:bos', label('BOS')) +
      p.row('st:choch', label('CHoCH')) +
      p.row('st:sweep', label('Sweep')) +
      p.row('st:fvg', label('FVGs')) +
      p.row('st:ob', label('Order block')) +
      p.row('st:pd', label('Premium / discount')) +
      p.row('st:ell', label('Elliott candidate')) +
      p.row('st:ellphase', label('Elliott phase')) +
      p.row('st:sup', label('Nearest support')) +
      p.row('st:res', label('Nearest resistance')));

    ['short', 'long'].forEach(function (side) {
      var rows = ['compression', 'fading_bounces', 'depth_drain', 'defender_pulling',
                  'attacker_stacking', 'flow_pressure', 'microprice_lean',
                  'velocity_rising', 'btc_alignment', 'repeated_tests']
        .map(function (name) {
          return '<div class="le-feature"><span>' + esc(name.replace(/_/g, ' ')) + '</span>' +
            '<span class="bar"><i data-le="F:' + side + ':' + name + ':bar"></i></span>' +
            '<span class="num le-num" data-le="F:' + side + ':' + name + '">—</span></div>';
        }).join('');
      html += card(side === 'long' ? 'Pre-break long' : 'Pre-break short',
        p.row('F:' + side + ':level', label('Level')) +
        p.row('F:' + side + ':tests', label('Tests')) +
        p.row('F:' + side + ':score', label('Break score')) +
        '<div class="le-note" data-le="F:' + side + ':note"></div>' + rows);
    });

    html += card('Virtual trades',
      '<div class="le-note">Бумажный учёт по собственным сигналам движка. ' +
      'Вход - только по первому свежему стакану ПОСЛЕ сигнала, выход - по ' +
      'стопу, цели или сроку удержания. Комиссии и проскальзывание вычтены. ' +
      'Если поток прерывается, цена исполнения не выдумывается: сделка ' +
      'помечается как неисполненная. Ордера на биржу не отправляются.</div>' +
      p.row('vt:open', label('Open positions')) +
      p.row('vt:pending', label('Waiting for a fill')) +
      p.row('vt:closed', label('Closed trades')) +
      p.row('vt:winrate', label('Win rate')) +
      p.row('vt:gross', label('Gross P&L')) +
      p.row('vt:fees', label('Fees paid')) +
      p.row('vt:net', label('Net P&L')) +
      p.row('vt:openpnl', label('Open P&L')) +
      p.row('vt:costs', label('Costs vs gross')) +
      p.row('vt:abandoned', label('Not filled - stream gap')) +
      p.row('vt:uncertain', label('Uncertain exits')) +
      p.row('vt:stale', label('Skipped - stale book')) +
      '<div class="le-subhead">' + esc(label('Journal')) + '</div>' +
      '<div class="le-journal" data-le="vt:journal"></div>');

    html += card('Feed health',
      clocks(p) +
      p.row('h:status', label('Engine status')) +
      p.row('h:signals', label('Signals')) +
      p.row('h:ws', label('WS connected')) +
      p.row('h:synced', label('Book synced')) +
      p.row('h:dropped', label('Dropped messages')) +
      p.row('h:reconnects', label('Reconnects')) +
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

    /* virtual trades - the engine's own signals, marked to the book */
    var vt = frame.virtual_trades || {};
    p.set('vt:open', vt.open_positions === undefined ? '—' : vt.open_positions);
    p.set('vt:pending', vt.pending_intents === undefined ? '—' : vt.pending_intents);
    p.set('vt:closed', vt.closed_trades === undefined ? '—' : vt.closed_trades);
    p.set('vt:winrate', vt.win_rate_pct === null || vt.win_rate_pct === undefined
      ? '—' : fmt(vt.win_rate_pct, 1) + '% (' + (vt.wins || 0) + '/' +
        ((vt.wins || 0) + (vt.losses || 0)) + ')');
    p.set('vt:gross', signed(vt.gross_pnl, 3));
    p.colour('vt:gross', vt.gross_pnl, 'generic');
    // Fees are a cost, so they are shown as one - never as a bare
    // positive number next to a P&L it was subtracted from.
    p.set('vt:fees', vt.fees_paid ? '-' + fmt(vt.fees_paid, 3) : '0.000');
    p.set('vt:net', signed(vt.net_pnl, 3));
    p.colour('vt:net', vt.net_pnl, 'generic');
    p.set('vt:openpnl', signed(vt.open_pnl, 3));
    p.colour('vt:openpnl', vt.open_pnl, 'generic');
    p.set('vt:costs', vt.fees_vs_gross_pct === null || vt.fees_vs_gross_pct === undefined
      ? '—' : fmt(vt.fees_vs_gross_pct, 1) + '%');
    p.set('vt:abandoned', vt.abandoned_on_gap || 0);
    p.set('vt:uncertain', vt.gap_uncertain_exits || 0);
    p.set('vt:stale', vt.skipped_stale_book || 0);
    p.html('vt:journal', journalRows(vt.recent));
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
