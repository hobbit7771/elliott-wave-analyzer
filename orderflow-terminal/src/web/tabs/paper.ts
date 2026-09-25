// Paper trading (simulated fills on the live book, never sent anywhere) and event backtests on recorded data.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, esc, fmtQ, fmtP, fmtDateTime, loadPref, savePref, loadPrefRaw, KIND_LABEL } from '../util.js';
import { PaperBook, backtestEvents, computeStats, type PaperStats, type ClosedTrade } from '../../core/paper.js';
import type { EventKind, MarketEvent, Trade } from '../../core/types.js';

interface Prefs {
  qty: number;
  slTicks: number;
  tpTicks: number;
  fee: number;
  slip: number;
}
const DEF: Prefs = { qty: 0.01, slTicks: 100, tpTicks: 200, fee: 0.05, slip: 1 };

export function createPaperTab(): Tab {
  const root = el('section', { id: 'tab-paper', role: 'tabpanel' });
  const p = loadPref<Prefs>('paperPrefs', DEF);
  const num = (v: number, step = 'any', title = '') => el('input', { type: 'number', min: '0', step, value: String(v), title });
  const qtyIn = num(p.qty);
  const slIn = num(p.slTicks, '1', 'Stop distance in ticks (0 = none)');
  const tpIn = num(p.tpTicks, '1', 'Target distance in ticks (0 = none)');
  const feeIn = num(p.fee, '0.001', 'Taker fee %');
  const slipIn = num(p.slip, '1', 'Slippage ticks on market fills and stops');
  const buyBtn = el('button', { class: 'buy', text: 'Paper BUY' });
  const sellBtn = el('button', { class: 'sell', text: 'Paper SELL' });
  const resetBtn = el('button', { text: 'Reset journal' });
  root.append(
    el('div', { class: 'toolbar' }, el('label', {}, 'Qty', qtyIn), el('label', {}, 'SL ticks', slIn), el('label', {}, 'TP ticks', tpIn), el('label', {}, 'Fee %', feeIn), el('label', {}, 'Slip ticks', slipIn), buyBtn, sellBtn, resetBtn),
  );
  const body = el('div', { class: 'scroll pad' });
  root.append(body);
  const banner = el('p', { class: 'muted', text: 'Paper trading only: orders are simulated against the live book (market fills at the opposite touch + slippage, fees, funding for perpetuals). Nothing is ever sent to an exchange.' });
  const cards = el('div', { class: 'cards' });
  const posBox = el('div');
  const journalBox = el('div');
  const btBox = el('div');
  body.append(banner, cards, el('h3', { text: 'Open positions' }), posBox, el('h3', { text: 'Journal' }), journalBox, el('h3', { text: 'Backtest detector events on recorded data' }), btBox);

  let book = new PaperBook(costs());
  let loadedKey = '';
  let lastFundingT = 0;
  function costs() {
    return { takerFee: (+feeIn.value || 0) / 100, slippageTicks: +slipIn.value || 0, tick: store.meta?.tickSize ?? 0.01, assumedSpreadTicks: 1 };
  }
  const key = () => 'paper:' + store.key;
  function persist(): void {
    savePref(key(), book.toJSON());
    window.dispatchEvent(new Event('paper-changed'));
  }
  function load(): void {
    book = new PaperBook(costs());
    book.load(loadPrefRaw(key(), { open: [], closed: [], seq: 1 }));
    loadedKey = store.key;
  }
  function touch(): { bid: number; ask: number } | null {
    const b = store.book;
    if (!b || !b.bids.length || !b.asks.length || store.status?.state !== 'connected') return null;
    return { bid: b.bids[0][0], ask: b.asks[0][0] };
  }
  function enter(side: 1 | -1): void {
    const t = touch();
    if (!t) return alert('No live, synced order book — paper orders need a real bid/ask.');
    const qty = +qtyIn.value;
    if (!(qty > 0)) return alert('Quantity must be > 0');
    const tick = store.meta?.tickSize ?? 0.01;
    const ref = side === 1 ? t.ask : t.bid;
    const sl = +slIn.value ? ref - side * +slIn.value * tick : undefined;
    const tp = +tpIn.value ? ref + side * +tpIn.value * tick : undefined;
    book.costs = costs();
    book.enter(side, qty, t.bid, t.ask, store.now(), sl, tp, 'manual');
    persist();
    render();
  }
  buyBtn.onclick = () => enter(1);
  sellBtn.onclick = () => enter(-1);
  resetBtn.onclick = () => {
    if (!confirm('Clear the paper journal for ' + store.symbol + '?')) return;
    book = new PaperBook(costs());
    persist();
    render();
  };
  for (const i of [qtyIn, slIn, tpIn, feeIn, slipIn])
    i.addEventListener('change', () => {
      Object.assign(p, { qty: +qtyIn.value, slTicks: +slIn.value, tpTicks: +tpIn.value, fee: +feeIn.value, slip: +slipIn.value });
      savePref('paperPrefs', p);
    });

  function statCards(s: PaperStats, unreal: number): void {
    const c = (k: string, v: string, cls = '') => el('div', { class: 'card' }, el('div', { class: 'k', text: k }), el('div', { class: 'v ' + cls, text: v }));
    cards.replaceChildren(
      c('Net P&L', fmtQ(s.net), s.net >= 0 ? 'pos' : 'neg'),
      c('Unrealized', fmtQ(unreal), unreal >= 0 ? 'pos' : 'neg'),
      c('Trades', String(s.trades)),
      c('Win rate', (s.winRate * 100).toFixed(1) + '%'),
      c('Expectancy / trade', fmtQ(s.expectancy)),
      c('Profit factor', isFinite(s.profitFactor) ? s.profitFactor.toFixed(2) : s.trades ? '∞' : '–'),
      c('Max drawdown', fmtQ(s.maxDrawdown)),
      c('Fees / funding', `${fmtQ(s.fees)} / ${fmtQ(s.funding)}`),
    );
  }

  function tradeRows(list: ClosedTrade[], d: number): string {
    return (
      '<table><thead><tr><th class="l">Entry (UTC)</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th class="l">Reason</th><th>Gross</th><th>Fees</th><th>Funding</th><th>Net</th><th class="l">Note</th></tr></thead><tbody>' +
      [...list]
        .reverse()
        .slice(0, 300)
        .map(
          (c) =>
            `<tr class="${c.side === 1 ? 'buy' : 'sell'}"><td class="l">${fmtDateTime(c.entryT)}</td><td class="side">${c.side === 1 ? 'long' : 'short'}</td><td>${fmtQ(c.qty)}</td><td>${fmtP(c.entry, d)}</td><td>${fmtP(c.exit, d)}</td><td class="l">${c.reason}</td><td>${fmtQ(c.gross)}</td><td>${fmtQ(c.fees)}</td><td>${fmtQ(c.funding)}</td><td class="${c.net >= 0 ? 'pos' : 'neg'}">${fmtQ(c.net)}</td><td class="l">${esc(c.note ?? '')}</td></tr>`,
        )
        .join('') +
      '</tbody></table>'
    );
  }

  function render(): void {
    if (loadedKey !== store.key) load();
    const t = touch();
    const d = store.meta?.pricePrecision ?? 2;
    statCards(book.stats(), t ? book.unrealized(t.bid, t.ask) : 0);
    posBox.innerHTML = book.open.length
      ? '<table><thead><tr><th>Side</th><th>Qty</th><th>Entry</th><th>SL</th><th>TP</th><th>Unrealized</th><th></th></tr></thead><tbody>' +
        book.open
          .map((o) => {
            const u = t ? ((o.side === 1 ? t.bid : t.ask) - o.entry) * o.side * o.qty - o.fees - o.funding : NaN;
            return `<tr class="${o.side === 1 ? 'buy' : 'sell'}"><td class="side">${o.side === 1 ? 'long' : 'short'}</td><td>${fmtQ(o.qty)}</td><td>${fmtP(o.entry, d)}</td><td>${o.sl !== undefined ? fmtP(o.sl, d) : '–'}</td><td>${o.tp !== undefined ? fmtP(o.tp, d) : '–'}</td><td class="${u >= 0 ? 'pos' : 'neg'}">${fmtQ(u)}</td><td><button data-close="${o.id}">Close</button></td></tr>`;
          })
          .join('') +
        '</tbody></table>'
      : '<p class="muted">No open paper positions.</p>';
    for (const b of posBox.querySelectorAll<HTMLButtonElement>('button[data-close]'))
      b.onclick = () => {
        const tt = touch();
        if (!tt) return alert('No live book to close against.');
        book.exit(+b.dataset.close!, tt.bid, tt.ask, store.now(), 'manual');
        persist();
        render();
      };
    journalBox.innerHTML = book.closed.length ? tradeRows(book.closed, d) : '<p class="muted">No closed paper trades yet.</p>';
  }

  // live marking: stops / targets / funding
  function onBook(): void {
    if (loadedKey !== store.key) load();
    const t = touch();
    if (!t) return;
    let changed = book.mark(t.bid, t.ask, store.now()).length > 0;
    const nf = store.deriv.nextFunding;
    const rate = store.deriv.funding;
    const mark = store.deriv.mark;
    // funding is charged when the exchange's next funding time rolls over
    if (nf && rate !== undefined && mark && lastFundingT && nf > lastFundingT && book.open.length) {
      book.applyFunding(rate, mark);
      changed = true;
    }
    if (nf) lastFundingT = nf;
    if (changed) persist();
    if (visible) render();
  }

  // ---------- backtest ----------
  const kinds = el('select', { multiple: 'true', size: '5', 'aria-label': 'Event types' });
  for (const [k, l] of Object.entries(KIND_LABEL)) if (k !== 'feed') kinds.append(el('option', { value: k, text: l, ...(k === 'iceberg' || k === 'absorption' ? { selected: 'true' } : {}) }));
  const btConf = num(60, '5');
  const btMode = el('select');
  btMode.append(el('option', { value: 'follow', text: 'follow (long on bid/buy events)' }), el('option', { value: 'fade', text: 'fade' }));
  const btStop = num(100, '1');
  const btTarget = num(150, '1');
  const btHold = num(15, '1', 'max hold minutes');
  const btRange = el('select');
  for (const [v, l] of [['3600000', 'last 1h'], ['14400000', 'last 4h'], ['21600000', 'last 6h']]) btRange.append(el('option', { value: v, text: l }));
  const btRun = el('button', { text: 'Run backtest' });
  const btOut = el('div');
  btBox.append(
    el('div', { class: 'form-grid' }, el('label', {}, 'Events', kinds), el('label', {}, 'Min confidence', btConf), el('label', {}, 'Mode', btMode), el('label', {}, 'Stop ticks', btStop), el('label', {}, 'Target ticks', btTarget), el('label', {}, 'Max hold (min)', btHold), el('label', {}, 'Range', btRange)),
    el('p', {}, btRun),
    el('p', { class: 'muted', text: 'Uses recorded trades and recorded events for this instrument only (server retention). Entries fill at the first trade after the event ± half a tick spread + slippage; stops/targets are checked on trade prices. One position at a time.' }),
    btOut,
  );
  btRun.onclick = async () => {
    btRun.disabled = true;
    btOut.textContent = 'Loading recorded data…';
    try {
      const to = store.now();
      const from = to - +btRange.value;
      const [tr, ev] = await Promise.all([
        api<{ trades: [number, number, number, 1 | -1, number][] }>('/api/trades', { source: store.source, symbol: store.symbol, from, to, limit: 300_000 }),
        api<MarketEvent[]>('/api/events', { source: store.source, symbol: store.symbol, from, to }),
      ]);
      const trades: Trade[] = tr.trades.map(([t, price, qty, side]) => ({ t, price, qty, side }));
      if (!trades.length) {
        btOut.textContent = 'No recorded trades in that range.';
        return;
      }
      const sel = [...kinds.selectedOptions].map((o) => o.value as EventKind);
      const r = backtestEvents(trades, ev, { kinds: sel, minConfidence: +btConf.value, mode: btMode.value as 'follow' | 'fade', stopTicks: +btStop.value, targetTicks: +btTarget.value, maxHoldMs: +btHold.value * 60_000, qty: +qtyIn.value || 1 }, costs());
      const s = computeStats(r.closed);
      btOut.innerHTML = `<p>${trades.length} trades, ${ev.length} events (${fmtDateTime(trades[0].t)} → ${fmtDateTime(trades[trades.length - 1].t)}). Signals skipped while in a position: ${r.skipped}.</p>
        <div class="cards">${[
          ['Trades', String(s.trades)],
          ['Win rate', (s.winRate * 100).toFixed(1) + '%'],
          ['Net', fmtQ(s.net)],
          ['Expectancy', fmtQ(s.expectancy)],
          ['Profit factor', isFinite(s.profitFactor) ? s.profitFactor.toFixed(2) : s.trades ? '∞' : '–'],
          ['Max DD', fmtQ(s.maxDrawdown)],
          ['Fees', fmtQ(s.fees)],
        ]
          .map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`)
          .join('')}</div>` + (r.closed.length ? tradeRows(r.closed, store.meta?.pricePrecision ?? 2) : '<p class="muted">No qualifying events in that range.</p>');
    } catch (e) {
      btOut.textContent = 'Backtest failed: ' + (e as Error).message;
    } finally {
      btRun.disabled = false;
    }
  };

  let visible = false;
  let bookTimer = 0;
  store.on('book', () => {
    if (!bookTimer)
      bookTimer = window.setTimeout(() => {
        bookTimer = 0;
        onBook();
      }, 250);
  });
  store.on('reset', () => {
    load();
    if (visible) render();
  });
  return {
    id: 'paper',
    title: 'Paper',
    root,
    show() {
      visible = true;
      render();
    },
    hide() {
      visible = false;
    },
  };
}
