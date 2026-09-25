// Paper/Backtest: заявки исполняются в серверном симуляторе по видимой глубине живого стакана
// (никогда не отправляются на биржу); SL/TP проверяет сервер, состояние хранится в Supabase.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, esc, fmtQ, fmtP, fmtDateTime, loadPref, savePref, KIND_LABEL, ownerToken } from '../util.js';
import { backtestEvents, computeStats, type ClosedTrade, type PaperStats } from '../../core/paper.js';
import type { EventKind, MarketEvent, Trade } from '../../core/types.js';

interface Prefs {
  qty: number;
  slTicks: number;
  tpTicks: number;
}
interface ServerPos {
  id: number;
  key: string;
  side: 1 | -1;
  qty: number;
  entry: number;
  entryT: number;
  sl?: number;
  tp?: number;
  fees: number;
  funding: number;
  note?: string;
}
interface ServerClosed extends ServerPos {
  exit: number;
  exitT: number;
  reason: string;
  gross: number;
  net: number;
  delayed?: boolean;
  fillNote?: string;
}
interface PaperView {
  cfg: { takerFee: number; extraSlippageTicks: number };
  open: ServerPos[];
  closed: ServerClosed[];
  outages: { key: string; t0: number; t1: number | null; reason: string }[];
  stats: PaperStats;
  unrealized: Record<number, number>;
}

const DEF: Prefs = { qty: 0.01, slTicks: 100, tpTicks: 200 };

export function createPaperTab(): Tab {
  const root = el('section', { id: 'tab-paper', role: 'tabpanel' });
  const p = loadPref<Prefs>('paperPrefs2', DEF);
  const num = (v: number, step = 'any', title = '') => el('input', { type: 'number', min: '0', step, value: String(v), title });
  const qtyIn = num(p.qty);
  const slIn = num(p.slTicks, '1', 'Стоп в тиках (0 — без стопа)');
  const tpIn = num(p.tpTicks, '1', 'Тейк в тиках (0 — без тейка)');
  const feeIn = num(0.05, '0.001', 'Комиссия taker, %');
  const slipIn = num(0, '1', 'Доп. проскальзывание в тиках поверх прохода по стакану');
  const buyBtn = el('button', { class: 'buy', text: 'Paper BUY' });
  const sellBtn = el('button', { class: 'sell', text: 'Paper SELL' });
  const cfgBtn = el('button', { text: 'Сохранить комиссии' });
  const resetBtn = el('button', { text: 'Сбросить журнал' });
  const msg = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, el('label', {}, 'Кол-во', qtyIn), el('label', {}, 'SL тиков', slIn), el('label', {}, 'TP тиков', tpIn), buyBtn, sellBtn, el('span', { class: 'sep' }), el('label', {}, 'Комиссия %', feeIn), el('label', {}, 'Доп. проск.', slipIn), cfgBtn, resetBtn, msg));
  const body = el('div', { class: 'scroll pad' });
  root.append(body);
  const banner = el('p', {
    class: 'muted',
    text: 'Только симуляция. Рыночная заявка проходит по видимым уровням живого стакана на сервере (средняя цена исполнения, без заполнения крупного объёма по лучшей цене); если видимой глубины не хватает или данные недостоверны (stale/gap/resync) — заявка отклоняется. SL/TP проверяет сервер, пока Render работает; после сна или разрыва стоп исполняется по стакану на момент восстановления и помечается «с задержкой». Funding начисляется при смене периода funding по mark-цене. Ничего не отправляется на биржу.',
  });
  const cards = el('div', { class: 'cards' });
  const posBox = el('div');
  const outBox = el('div');
  const journalBox = el('div');
  const btBox = el('div');
  body.append(banner, cards, el('h3', { text: 'Открытые позиции' }), posBox, outBox, el('h3', { text: 'Журнал' }), journalBox, el('h3', { text: 'Бэктест событий детекторов на сохранённых данных' }), btBox);

  let view: PaperView | null = null;
  let visible = false;
  const dec = () => store.meta?.pricePrecision ?? 2;

  async function refresh(): Promise<void> {
    if (!ownerToken()) {
      msg.textContent = 'Нужен токен владельца (вкладка «Источники и настройки»).';
      return;
    }
    try {
      view = await api<PaperView>('/api/paper');
      feeIn.value = String(+(view.cfg.takerFee * 100).toFixed(4));
      slipIn.value = String(view.cfg.extraSlippageTicks);
      render();
      window.dispatchEvent(new Event('paper-changed'));
    } catch (e) {
      msg.textContent = (e as Error).message;
    }
  }

  function statCards(s: PaperStats, unreal: number): void {
    const c = (k: string, v: string, cls = '') => el('div', { class: 'card' }, el('div', { class: 'k', text: k }), el('div', { class: 'v ' + cls, text: v }));
    cards.replaceChildren(
      c('Итог P&L', fmtQ(s.net), s.net >= 0 ? 'pos' : 'neg'),
      c('Нереализованный', fmtQ(unreal), unreal >= 0 ? 'pos' : 'neg'),
      c('Сделок', String(s.trades)),
      c('Win rate', (s.winRate * 100).toFixed(1) + '%'),
      c('Expectancy', fmtQ(s.expectancy)),
      c('Profit factor', Number.isFinite(s.profitFactor) ? s.profitFactor.toFixed(2) : s.trades ? '∞' : '–'),
      c('Макс. просадка', fmtQ(s.maxDrawdown)),
      c('Комиссии / funding', `${fmtQ(s.fees)} / ${fmtQ(s.funding)}`),
    );
  }

  function rows(list: (ClosedTrade | ServerClosed)[]): string {
    return (
      '<table><thead><tr><th class="l">Вход (UTC)</th><th class="l">Инструмент</th><th>Сторона</th><th>Кол-во</th><th>Вход</th><th>Выход</th><th class="l">Причина</th><th>Gross</th><th>Комиссии</th><th>Funding</th><th>Net</th><th class="l">Исполнение</th></tr></thead><tbody>' +
      [...list]
        .reverse()
        .slice(0, 300)
        .map((c) => {
          const sc = c as ServerClosed;
          return `<tr class="${c.side === 1 ? 'buy' : 'sell'}"><td class="l">${fmtDateTime(c.entryT)}</td><td class="l">${esc(sc.key ?? store.symbol)}</td><td class="side">${c.side === 1 ? 'long' : 'short'}</td><td>${fmtQ(c.qty)}</td><td>${fmtP(c.entry, dec())}</td><td>${fmtP(c.exit, dec())}</td><td class="l">${c.reason}${sc.delayed ? ' (с задержкой)' : ''}</td><td>${fmtQ(c.gross)}</td><td>${fmtQ(c.fees)}</td><td>${fmtQ(c.funding)}</td><td class="${c.net >= 0 ? 'pos' : 'neg'}">${fmtQ(c.net)}</td><td class="l">${esc(sc.fillNote ?? c.note ?? '')}</td></tr>`;
        })
        .join('') +
      '</tbody></table>'
    );
  }

  function render(): void {
    if (!view) return;
    const unreal = Object.values(view.unrealized).reduce((s, x) => s + x, 0);
    statCards(view.stats, unreal);
    posBox.innerHTML = view.open.length
      ? '<table><thead><tr><th class="l">Инструмент</th><th>Сторона</th><th>Кол-во</th><th>Вход</th><th>SL</th><th>TP</th><th>Нереал.</th><th></th></tr></thead><tbody>' +
        view.open
          .map((o) => {
            const u = view!.unrealized[o.id];
            return `<tr class="${o.side === 1 ? 'buy' : 'sell'}"><td class="l">${esc(o.key)}</td><td class="side">${o.side === 1 ? 'long' : 'short'}</td><td>${fmtQ(o.qty)}</td><td>${fmtP(o.entry, dec())}</td><td>${o.sl !== undefined ? fmtP(o.sl, dec()) : '–'}</td><td>${o.tp !== undefined ? fmtP(o.tp, dec()) : '–'}</td><td class="${(u ?? 0) >= 0 ? 'pos' : 'neg'}">${u === undefined ? 'нет данных' : fmtQ(u)}</td><td><button data-close="${o.id}">Закрыть</button></td></tr>`;
          })
          .join('') +
        '</tbody></table>'
      : '<p class="muted">Открытых позиций нет.</p>';
    for (const b of posBox.querySelectorAll<HTMLButtonElement>('button[data-close]'))
      b.onclick = async () => {
        try {
          await api('/api/paper/close', {}, { method: 'POST', body: JSON.stringify({ id: +b.dataset.close! }) });
          msg.textContent = '';
        } catch (e) {
          msg.textContent = 'Не закрыто: ' + (e as Error).message;
        }
        void refresh();
      };
    const outs = view.outages.filter((o) => o.t1 === null || Date.now() - o.t1 < 86_400_000);
    outBox.innerHTML = outs.length ? '<p class="warn">Периоды, когда сервер не мог проверять SL/TP: ' + outs.map((o) => `${esc(o.key)} ${fmtDateTime(o.t0)} → ${o.t1 ? fmtDateTime(o.t1) : 'сейчас'} (${esc(o.reason)})`).join('; ') + '</p>' : '';
    journalBox.innerHTML = view.closed.length ? rows(view.closed) : '<p class="muted">Закрытых сделок пока нет.</p>';
  }

  async function order(side: 1 | -1): Promise<void> {
    const b = store.book;
    const tick = store.meta?.tickSize ?? 0.01;
    if (!b || !b.bids.length || !b.asks.length) {
      msg.textContent = 'Нет живого стакана.';
      return;
    }
    const ref = side === 1 ? b.asks[0][0] : b.bids[0][0];
    const sl = +slIn.value ? +(ref - side * +slIn.value * tick).toFixed(dec()) : undefined;
    const tp = +tpIn.value ? +(ref + side * +tpIn.value * tick).toFixed(dec()) : undefined;
    try {
      await api('/api/paper/order', {}, { method: 'POST', body: JSON.stringify({ source: store.source, symbol: store.symbol, side, qty: +qtyIn.value, sl, tp }) });
      msg.textContent = 'Исполнено в симуляторе.';
    } catch (e) {
      msg.textContent = 'Отклонено: ' + (e as Error).message;
    }
    void refresh();
  }
  buyBtn.onclick = () => void order(1);
  sellBtn.onclick = () => void order(-1);
  cfgBtn.onclick = async () => {
    await api('/api/paper/config', {}, { method: 'POST', body: JSON.stringify({ takerFee: (+feeIn.value || 0) / 100, extraSlippageTicks: +slipIn.value || 0 }) }).catch((e) => (msg.textContent = (e as Error).message));
    void refresh();
  };
  resetBtn.onclick = async () => {
    if (!confirm('Очистить весь paper-журнал на сервере?')) return;
    await api('/api/paper/reset', {}, { method: 'POST', body: '{}' }).catch((e) => (msg.textContent = (e as Error).message));
    void refresh();
  };
  for (const i of [qtyIn, slIn, tpIn]) i.addEventListener('change', () => savePref('paperPrefs2', { qty: +qtyIn.value, slTicks: +slIn.value, tpTicks: +tpIn.value }));

  // ---------- backtest on stored real data ----------
  const kinds = el('select', { multiple: 'true', size: '5', 'aria-label': 'Типы событий' });
  for (const [k, l] of Object.entries(KIND_LABEL)) if (k !== 'feed') kinds.append(el('option', { value: k, text: l, ...(k === 'iceberg' || k === 'absorption' ? { selected: 'true' } : {}) }));
  const btScore = num(60, '5');
  const btMode = el('select');
  btMode.append(el('option', { value: 'follow', text: 'по направлению (long на bid/buy)' }), el('option', { value: 'fade', text: 'против' }));
  const btStop = num(100, '1');
  const btTarget = num(150, '1');
  const btHold = num(15, '1', 'макс. удержание, мин');
  const btRange = el('select');
  for (const [v, l] of [['3600000', 'последний 1 ч'], ['14400000', 'последние 4 ч'], ['21600000', 'последние 6 ч']]) btRange.append(el('option', { value: v, text: l }));
  const btRun = el('button', { text: 'Запустить бэктест' });
  const btOut = el('div');
  btBox.append(
    el('div', { class: 'form-grid' }, el('label', {}, 'События', kinds), el('label', {}, 'Мин. score', btScore), el('label', {}, 'Режим', btMode), el('label', {}, 'Стоп, тиков', btStop), el('label', {}, 'Цель, тиков', btTarget), el('label', {}, 'Удержание, мин', btHold), el('label', {}, 'Период', btRange)),
    el('p', {}, btRun),
    el('p', {
      class: 'muted',
      text: 'Только по сохранённым реальным сделкам и событиям этого инструмента. Сигнал доступен с момента обнаружения (время события), вход — по первой сделке после него ± полтика спреда (исторической глубины стакана для бэктеста нет, поэтому исполнение приближённое); стоп/цель проверяются по ценам сделок. Одна позиция одновременно. Синтетические тесты не доказывают прибыльность.',
    }),
    btOut,
  );
  btRun.onclick = async () => {
    btRun.disabled = true;
    btOut.textContent = 'Загрузка сохранённых данных…';
    try {
      const to = store.now();
      const from = to - +btRange.value;
      const [tr, ev] = await Promise.all([
        api<{ trades: [number, number, number, 1 | -1, number][]; sources: string[] }>('/api/trades', { source: store.source, symbol: store.symbol, from, to, limit: 300_000 }),
        api<MarketEvent[]>('/api/events', { source: store.source, symbol: store.symbol, from, to }),
      ]);
      const trades: Trade[] = tr.trades.map(([t, price, qty, side]) => ({ t, price, qty, side }));
      if (!trades.length) {
        btOut.textContent = 'В этом периоде нет сохранённых сделок.';
        return;
      }
      const sel = [...kinds.selectedOptions].map((o) => o.value as EventKind);
      const r = backtestEvents(trades, ev, { kinds: sel, minConfidence: +btScore.value, mode: btMode.value as 'follow' | 'fade', stopTicks: +btStop.value, targetTicks: +btTarget.value, maxHoldMs: +btHold.value * 60_000, qty: +qtyIn.value || 1 }, { takerFee: (+feeIn.value || 0) / 100, slippageTicks: +slipIn.value || 0, tick: store.meta?.tickSize ?? 0.01, assumedSpreadTicks: 1 });
      const s = computeStats(r.closed);
      btOut.innerHTML =
        `<p>${trades.length} сделок, ${ev.length} событий (${fmtDateTime(trades[0].t)} → ${fmtDateTime(trades[trades.length - 1].t)}; источники: ${esc(tr.sources.join(', '))}). Пропущено сигналов при открытой позиции: ${r.skipped}.</p><div class="cards">${[
          ['Сделок', String(s.trades)],
          ['Win rate', (s.winRate * 100).toFixed(1) + '%'],
          ['Net', fmtQ(s.net)],
          ['Expectancy', fmtQ(s.expectancy)],
          ['Profit factor', Number.isFinite(s.profitFactor) ? s.profitFactor.toFixed(2) : s.trades ? '∞' : '–'],
          ['Макс. просадка', fmtQ(s.maxDrawdown)],
        ]
          .map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`)
          .join('')}</div>` + (r.closed.length ? rows(r.closed) : '<p class="muted">Подходящих событий в этом периоде нет.</p>');
    } catch (e) {
      btOut.textContent = 'Ошибка бэктеста: ' + (e as Error).message;
    } finally {
      btRun.disabled = false;
    }
  };

  let timer = 0;
  return {
    id: 'paper',
    title: 'Paper/Backtest',
    root,
    show() {
      visible = true;
      void refresh();
      timer = window.setInterval(() => visible && void refresh(), 3000);
    },
    hide() {
      visible = false;
      clearInterval(timer);
    },
  };
}
