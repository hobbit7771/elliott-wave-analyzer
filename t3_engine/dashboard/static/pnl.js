/* Where the algorithm actually trades, and what it has made.
 *
 * The complaint this answers, verbatim: "я не вижу где алгоритм торгует,
 * нет ни вкладки доходности ничего нет". Both books existed and both were
 * running; neither had a surface. A paper engine whose P&L you cannot see
 * is indistinguishable from one that is not trading at all.
 *
 * TWO BOOKS, SIDE BY SIDE, NEVER SUMMED. They are different strategies on
 * different horizons:
 *
 *   ПЕЙПЕР-ДВИЖОК  the Elliott strategy, on CLOSED candles, through the
 *                  same BacktestEngine a backtest uses. Its horizon is
 *                  hours to days, so an empty journal after twenty
 *                  minutes is the expected reading, not a fault - and the
 *                  panel says so rather than showing a blank.
 *   LEAD ENGINE    the microstructure ledger: its own signals marked
 *                  against the live order book, entry at the next fresh
 *                  book after the signal. Minutes.
 *
 * A single blended number would hide which of the two is working, so
 * there isn't one.
 *
 * Polls only while the tab is visible - see PnlTab.show/hide.
 */
(function (global) {
  'use strict';

  var REFRESH_MS = 3000;
  var timer = null;
  var visible = false;

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

  function tone(value) {
    var n = Number(value);
    if (!isFinite(n) || n === 0) return 'dim';
    return n > 0 ? 'up' : 'down';
  }

  function price(value) {
    if (value === undefined || value === null) return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return n.toFixed(n >= 1000 ? 1 : n >= 10 ? 3 : 5);
  }

  function clock(ms) {
    if (!ms) return '—';
    var d = new Date(Number(ms));
    if (isNaN(d.getTime())) return '—';
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) +
      ':' + ('0' + d.getSeconds()).slice(-2);
  }

  function since(ms) {
    if (!ms) return '—';
    var seconds = Math.max(0, (Date.now() - Number(ms)) / 1000);
    if (seconds < 90) return Math.round(seconds) + 'с';
    if (seconds < 5400) return Math.round(seconds / 60) + 'м';
    return (seconds / 3600).toFixed(1) + 'ч';
  }

  function row(label, value, cls) {
    return '<div class="le-row"><span class="k">' + esc(label) + '</span>' +
      '<span class="v le-num ' + (cls || '') + '">' + value + '</span></div>';
  }

  function card(title, inner, badge) {
    return '<div class="le-card"><h3>' + esc(title) +
      (badge ? ' <span class="le-chip">' + esc(badge) + '</span>' : '') +
      '</h3>' + inner + '</div>';
  }

  /* ---- the paper engine ---------------------------------------------- */

  var FILL_EVENT = {
    ENTRY: 'вход', TP_HIT: 'цель', STOP_LOSS: 'стоп', CLOSE: 'закрытие'
  };

  function paperCard(state) {
    // Equity is deliberately NOT summed across timeframes: every book
    // starts from the same nominal capital, so five 10 000 books are not
    // 50 000 of capital. What adds up is the money made.
    var head =
      row('Статус', state.running
            ? '<b class="up">торгует</b>'
            : '<b class="down">остановлен</b>') +
      row('Режим', 'бумажный (' + esc(state.live_source || '—') + ')') +
      row('Работает', esc(since(state.started_at))) +
      row('Сделок в потоке', num(state.trades_received, 0)) +
      row('Последняя цена', price(state.mark)) +
      row('Капитал на таймфрейм', num(state.equity_per_timeframe, 2)) +
      row('Закрытая прибыль', signed(state.realized_pnl, 4), tone(state.realized_pnl)) +
      row('Открытая прибыль', signed(state.unrealized_pnl, 4), tone(state.unrealized_pnl)) +
      row('Итого P&L', '<b>' + signed(state.pnl, 4) + '</b>', tone(state.pnl)) +
      row('Открыто позиций', num((state.open_positions || []).length, 0)) +
      row('Входов / выходов', num(state.entries, 0) + ' / ' + num(state.exits, 0)) +
      row('Целей / стопов', num(state.take_profits, 0) + ' / ' + num(state.stops, 0));

    if (state.error) {
      head += '<div class="le-note" style="color:#f87171;">Ошибка: ' +
              esc(state.error) + '</div>';
    }

    // Why an empty journal is not a fault. In ai_only mode a timeframe
    // cannot open anything until a saved count exists for it, and the
    // Elliott strategy trades CLOSED candles on 5m and up - so twenty
    // minutes of uptime is a handful of bars, not a handful of setups.
    var counted = 0, tfRows = '';
    Object.keys(state.timeframes || {}).forEach(function (tf) {
      var t = state.timeframes[tf];
      if (t.ai_count) counted += 1;
      tfRows += '<div class="le-jrow pnl-tf">' +
        '<span>' + esc(tf) + '</span>' +
        '<span class="le-num">' + num(t.equity, 2) + '</span>' +
        '<span class="le-num ' + tone(t.realized_pnl) + '">' + signed(t.realized_pnl, 4) + '</span>' +
        '<span class="le-num">' + num(t.open_positions, 0) + '/' + num(t.closed_positions, 0) + '</span>' +
        '<span class="le-num">' + num(t.candles, 0) + '</span>' +
        '<span>' + (t.ai_count ? 'есть' : '<span class="dim">нет</span>') + '</span>' +
        '</div>';
    });
    var tfBlock =
      '<div class="le-subhead">По таймфреймам</div>' +
      '<div class="le-journal">' +
      '<div class="le-jrow pnl-tf pnl-head"><span>ТФ</span><span>капитал</span>' +
      '<span>realized</span><span>откр/закр</span><span>свечей</span>' +
      '<span>разметка</span></div>' + tfRows + '</div>';

    if (!counted && state.ai_only) {
      tfBlock += '<div class="le-note">Ни на одном таймфрейме нет сохранённой ' +
        'AI-разметки, а сессия идёт в режиме ai_only — значит открывать позиции ' +
        'пока не по чему. Это не сбой движка: посчитайте волны во вкладке ' +
        'AI Analyst или Claude, и разметка начнёт торговаться на следующей ' +
        'закрытой свече.</div>';
    }

    var opens = (state.open_positions || []).map(function (p) {
      return '<div class="le-jrow pnl-pos">' +
        '<span class="j-side ' + (String(p.side).toUpperCase().indexOf('L') === 0 ? 'long' : 'short') + '">' +
          esc(p.side) + '</span>' +
        '<span>' + esc(p.timeframe || '') + '</span>' +
        '<span class="le-num">' + price(p.entry_price) + '</span>' +
        '<span class="le-num">' + price(p.stop_loss) + '</span>' +
        '<span class="le-num">' + num(p.quantity, 6) + '</span>' +
        '<span class="le-num ' + tone(p.unrealized_pnl) + '">' + signed(p.unrealized_pnl, 4) + '</span>' +
        '</div>';
    }).join('');
    var openBlock = opens
      ? '<div class="le-subhead">Открытые позиции</div>' +
        '<div class="le-journal">' +
        '<div class="le-jrow pnl-pos pnl-head"><span>сторона</span><span>ТФ</span>' +
        '<span>вход</span><span>стоп</span><span>объём</span><span>P&L</span></div>' +
        opens + '</div>'
      : '';

    var fills = (state.fills || []).map(function (f) {
      var label = FILL_EVENT[f.event] || f.event;
      if (f.label) label += ' ' + f.label;
      return '<div class="le-jrow pnl-fill">' +
        '<span class="j-time">' + esc(clock(f.at)) + '</span>' +
        '<span>' + esc(f.timeframe || '') + '</span>' +
        '<span>' + esc(label) + '</span>' +
        '<span class="le-num">' + price(f.price) + '</span>' +
        '<span class="le-num ' + tone(f.position_realized_pnl) + '">' +
          signed(f.position_realized_pnl, 4) + '</span>' +
        '</div>';
    }).join('');
    var fillBlock = fills
      ? '<div class="le-subhead">Журнал исполнений</div>' +
        '<div class="le-journal">' + fills + '</div>'
      : '<div class="le-note">Исполнений пока нет. Стратегия Эллиотта ' +
        'торгует по ЗАКРЫТЫМ свечам от 5 минут и выше, поэтому на коротком ' +
        'прогоне пустой журнал — ожидаемое состояние, а не отсутствие работы.</div>';

    var saved = state.journal || {};
    var savedBlock = saved.fills
      ? '<div class="le-note">За всю историю в базе: входов ' + num(saved.trades_opened, 0) +
        ', исполнений ' + num(saved.fills, 0) + ', целей ' + num(saved.take_profits_hit, 0) +
        ', стопов ' + num(saved.stops_hit, 0) + ', realized ' + signed(saved.realized_pnl, 4) +
        '. Эта строка переживает перезапуск, журнал выше — нет.</div>'
      : '';

    return card('Пейпер-движок — ' + state.symbol, head + tfBlock + openBlock +
                fillBlock + savedBlock, 'Эллиотт, закрытые свечи');
  }

  /* ---- the Lead Engine ledger ---------------------------------------- */

  var TRADE_STATUS = {
    PENDING: 'ждёт стакан', OPEN: 'открыта', CLOSED: 'закрыта',
    ABANDONED: 'не исполнена'
  };
  var EXIT_REASON = { stop: 'стоп', target: 'цель', time: 'по времени' };

  /* One card for the whole ledger, not one per symbol.

     Seven instruments meant seven cards, six of them empty, and on a
     phone that is a wall of zeroes with the one active book buried
     somewhere inside it. Summing ACROSS SYMBOLS is legitimate here in a
     way that summing across the two books is not: one strategy, one set
     of rules, one cost model - that is a portfolio, not a blend. */
  function leadCard(lead) {
    var names = Object.keys(lead);
    var t = { closed: 0, wins: 0, losses: 0, gross: 0, fees: 0, net: 0,
              open: 0, pending: 0, openPnl: 0, abandoned: 0,
              uncertain: 0, stale: 0, restored: 0 };
    var merged = [];
    names.forEach(function (name) {
      var s = (lead[name] || {}).summary || {};
      t.closed += s.closed_trades || 0;
      t.wins += s.wins || 0;
      t.losses += s.losses || 0;
      t.gross += s.gross_pnl || 0;
      t.fees += s.fees_paid || 0;
      t.net += s.net_pnl || 0;
      t.open += s.open_positions || 0;
      t.pending += s.pending_intents || 0;
      t.openPnl += s.open_pnl || 0;
      t.abandoned += s.abandoned_on_gap || 0;
      t.uncertain += s.gap_uncertain_exits || 0;
      t.stale += s.skipped_stale_book || 0;
      t.restored += s.restored_from_storage || 0;
      ((lead[name] || {}).journal || []).forEach(function (r) {
        merged.push({ row: r, symbol: name });
      });
    });
    merged.sort(function (a, b) {
      return (b.row.signal_at_ms || 0) - (a.row.signal_at_ms || 0);
    });

    var decided = t.wins + t.losses;
    var head =
      row('Инструментов', num(names.length, 0)) +
      row('Открыто позиций', num(t.open, 0)) +
      row('Ждут исполнения', num(t.pending, 0)) +
      row('Закрытых сделок', num(t.closed, 0)) +
      row('Доля прибыльных', decided
            ? num(100 * t.wins / decided, 1) + '% (' + t.wins + '/' + decided + ')'
            : '—') +
      row('P&L до издержек', signed(t.gross, 4), tone(t.gross)) +
      row('Комиссии', t.fees ? '-' + num(t.fees, 4) : '0.0000') +
      row('Чистый P&L', '<b>' + signed(t.net, 4) + '</b>', tone(t.net)) +
      row('P&L по открытым', signed(t.openPnl, 4), tone(t.openPnl)) +
      row('Издержки к прибыли', t.gross
            ? num(100 * t.fees / Math.abs(t.gross), 1) + '%' : '—') +
      row('Не исполнено — разрыв потока', num(t.abandoned, 0)) +
      row('Неточные выходы', num(t.uncertain, 0)) +
      row('Пропущено — устаревший стакан', num(t.stale, 0)) +
      // Without this "42 сделки" reads as 42 сделки за этот запуск, and
      // on a host that restarts every fifteen minutes that is almost
      // never what the number means.
      row('Из них до перезапуска', num(t.restored, 0));

    // Per symbol, but only the ones that did something. A row of zeroes
    // for an instrument that never signalled is noise; the COUNT of
    // quiet instruments is the information, and that is one line.
    var active = names.filter(function (name) {
      var s = (lead[name] || {}).summary || {};
      return (s.closed_trades || 0) || (s.open_positions || 0) ||
             (s.pending_intents || 0) || (s.abandoned || 0);
    });
    var perSymbol = active.map(function (name) {
      var s = (lead[name] || {}).summary || {};
      var d = (s.wins || 0) + (s.losses || 0);
      return '<div class="le-jrow pnl-sym">' +
        '<span>' + esc(name) + '</span>' +
        '<span class="le-num">' + num(s.closed_trades, 0) + '</span>' +
        '<span class="le-num">' + (d ? num(100 * (s.wins || 0) / d, 0) + '%' : '—') + '</span>' +
        '<span class="le-num ' + tone(s.net_pnl) + '">' + signed(s.net_pnl, 4) + '</span>' +
        '<span class="le-num">' + num(s.open_positions, 0) + '</span>' +
        '</div>';
    }).join('');
    var symBlock = perSymbol
      ? '<div class="le-subhead">По инструментам</div><div class="le-journal">' +
        '<div class="le-jrow pnl-sym pnl-head"><span>инструмент</span><span>сделок</span>' +
        '<span>winrate</span><span>чистый P&L</span><span>откр</span></div>' +
        perSymbol + '</div>'
      : '';
    var quiet = names.length - active.length;
    if (quiet > 0) {
      symBlock += '<div class="le-note">Ещё ' + quiet + ' ' +
        (quiet === 1 ? 'инструмент' : 'инструментов') +
        ' без сделок — сигналов, годных для входа, там не было.</div>';
    }

    var rows = merged.slice(0, 40).map(function (item) {
      var r = item.row;
      var net = r.net_pnl;
      var cls = r.status !== 'CLOSED' ? 'dim' : tone(net);
      return '<div class="le-jrow pnl-lead' +
        (r.status === 'ABANDONED' ? ' skipped' : '') + '">' +
        '<span class="j-time">' + esc(clock(r.signal_at_ms)) + '</span>' +
        '<span>' + esc(item.symbol) + '</span>' +
        '<span class="j-side ' + (r.direction === 'long' ? 'long' : 'short') + '">' +
          esc(r.direction === 'long' ? 'LONG' : 'SHORT') + '</span>' +
        '<span class="j-state">' + esc(r.signal_state || '') + '</span>' +
        '<span class="le-num">' + price(r.entry_price) + '</span>' +
        '<span class="le-num">' + price(r.exit_price) + '</span>' +
        '<span>' + esc(r.status === 'CLOSED'
              ? (EXIT_REASON[r.exit_reason] || r.exit_reason || '')
              : (TRADE_STATUS[r.status] || r.status)) +
          (r.gap_uncertain ? ' <b title="выход оценён через разрыв потока">~</b>' : '') +
        '</span>' +
        '<span class="le-num ' + cls + '">' +
          (r.status === 'CLOSED' ? signed(net, 4) : '—') + '</span>' +
        '</div>';
    }).join('');

    var journal = rows
      ? '<div class="le-subhead">Журнал</div><div class="le-journal">' +
        '<div class="le-jrow pnl-lead pnl-head"><span>время</span><span>инстр.</span>' +
        '<span>сторона</span><span>сигнал</span><span>вход</span><span>выход</span>' +
        '<span>причина</span><span>P&L</span></div>' + rows + '</div>'
      : '<div class="le-note">Сигналов, годных для входа, пока не было. ' +
        'Журнал наполняется только состояниями PRE_BREAK / HIGH_PROBABILITY / ' +
        'A_PLUS / REVERSAL_CANDIDATE — WATCH и IDLE не торгуются.</div>';

    var cfg = {};
    names.some(function (name) {
      var c = ((lead[name] || {}).summary || {}).config;
      if (c) { cfg = c; return true; }
      return false;
    });
    var costs = '<div class="le-note">Издержки в расчёте: тейкер ' +
      num((cfg.taker_fee || 0) * 100, 3) + '% в обе стороны, проскальзывание ' +
      num(cfg.slippage_bps, 1) + ' bps, объём ' + num(cfg.notional, 0) +
      '. Вход — только по первому свежему стакану ПОСЛЕ сигнала; если поток ' +
      'прервался дольше ' + num(cfg.max_fill_gap_ms, 0) + ' мс, сделка ' +
      'помечается неисполненной, а не исполняется по выдуманной цене.</div>';

    return card('Lead Engine — виртуальный журнал', head + symBlock + journal + costs,
                'микроструктура, стакан');
  }

  /* ---- render --------------------------------------------------------- */

  function render(data) {
    var body = document.getElementById('pnlBody');
    if (!body) return;
    var html = '';

    html += '<div class="le-note"><b>Бумажный режим.</b> ' +
      esc(data.disclaimer || '') + '</div>';

    var paper = data.paper || {};
    var lead = data.lead || {};
    var names = Object.keys(paper);
    var leadNames = Object.keys(lead);

    if (!names.length && !leadNames.length) {
      html += '<div class="le-note">Ни один движок не запущен. Автостарт ' +
        'настраивается переменной LIVE_AUTOSTART_SYMBOLS; сейчас в ней: ' +
        (data.autostart && data.autostart.length
          ? esc(data.autostart.join(', ')) : '<i>пусто</i>') + '.</div>';
    }

    html += '<div class="le-grid">';
    names.forEach(function (name) { html += paperCard(paper[name]); });
    if (leadNames.length) html += leadCard(lead);
    html += '</div>';

    html += '<div class="le-note">Два журнала считаются отдельно и никогда ' +
      'не складываются: это разные стратегии на разных горизонтах — ' +
      'Эллиотт на закрытых свечах против микроструктуры на стакане. ' +
      'Общая цифра скрыла бы, какая из двух работает.</div>';

    html += '<div class="le-note">Журнал Lead Engine сохраняется в базу и ' +
      'переживает перезапуск: строка «из них до перезапуска» говорит, ' +
      'какая часть выборки старше текущего процесса. Пейпер-движок ' +
      'состояние пока не сохраняет — его позиции живут только в памяти.</div>';

    body.innerHTML = html;
  }

  function poll() {
    fetch('/api/live/performance', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        var body = document.getElementById('pnlBody');
        if (body) {
          body.innerHTML = '<div class="le-note" style="color:#f87171;">' +
            'Не удалось прочитать /api/live/performance: ' +
            esc(e && e.message ? e.message : e) + '</div>';
        }
      });
  }

  function show() {
    if (visible) return;
    visible = true;
    poll();
    timer = setInterval(poll, REFRESH_MS);
  }

  function hide() {
    visible = false;
    if (timer) { clearInterval(timer); timer = null; }
  }

  global.PnlTab = { show: show, hide: hide, refresh: poll };
})(window);
