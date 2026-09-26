import './styles.css';
import { store, type BookMsg, type StatusX } from './store.js';
import { api, el, ago, fmtQ, fmtP, savePref, loadPrefRaw } from './util.js';
import { TIMEFRAMES, TF_LABEL, type Timeframe } from '../core/candles.js';
import type { HeatColumn, InstrumentMeta, MarketEvent, SourceId, Trade } from '../core/types.js';
import { createChartTab } from './tabs/chart.js';
import { createHeatmapTab } from './tabs/heatmap.js';
import { createDomTab } from './tabs/dom.js';
import { createFootprintTab } from './tabs/footprint.js';
import { createProfileTab } from './tabs/profile.js';
import { createSignalsTab } from './tabs/signals.js';
import { createLevelsTab } from './tabs/levels.js';
import { createPaperTab } from './tabs/paper.js';
import { createAlertsTab, alerts } from './tabs/alerts.js';
import { createSourcesTab } from './tabs/sources.js';

export interface Tab {
  id: string;
  title: string;
  root: HTMLElement;
  show(): void;
  hide(): void;
}

const app = document.getElementById('app')!;

// ---------- header ----------
const sourceSel = el('select', { 'aria-label': 'Источник данных' });
sourceSel.append(el('option', { value: 'binance-futures', text: 'Binance Futures' }), el('option', { value: 'binance-spot', text: 'Binance Spot' }), el('option', { value: 'bybit-linear', text: 'Bybit Perp' }));
const symbolInput = el('input', { id: 'symbolInput', list: 'symbols', 'aria-label': 'Инструмент', autocomplete: 'off', spellcheck: 'false' });
const symbolList = el('datalist', { id: 'symbols' });
const tfGroup = el('div', { class: 'tf-group', role: 'group', 'aria-label': 'Timeframe' });
for (const tf of TIMEFRAMES) {
  const b = el('button', { 'data-tf': tf, text: TF_LABEL[tf] });
  b.onclick = () => setTf(tf);
  tfGroup.append(b);
}
const tfSelect = el('select', { class: 'tf-select', 'aria-label': 'Timeframe' });
for (const tf of TIMEFRAMES) tfSelect.append(el('option', { value: tf, text: TF_LABEL[tf] }));
tfSelect.onchange = () => setTf(tfSelect.value as Timeframe);
const ticksSel = el('select', { 'aria-label': 'Сделок в тиковом баре', title: 'Сообщений aggTrade в тиковом баре (одно сообщение может объединять несколько исполнений)' });
for (const n of [50, 100, 250, 500, 1000]) ticksSel.append(el('option', { value: String(n), text: `${n}t` }));
const header = el('header', { class: 'top' }, el('div', { class: 'brand' }, 'OrderFlow', el('small', {}, ' Terminal · только анализ')), sourceSel, symbolInput, symbolList, tfGroup, tfSelect, ticksSel);

// ---------- status bar ----------
const sb = {
  badge: el('span', { class: 'badge connecting', text: 'Подключение' }),
  fresh: el('span', { class: 'fresh', title: 'Свежесть данных стакана' }),
  hist: el('span', { class: 'hist' }),
  last: el('b', { text: '–' }),
  lat: el('b', { text: '–' }),
  rtt: el('b', { text: '–' }),
  trades: el('b', { text: '0' }),
  depth: el('b', { text: '0' }),
  src: el('b', { text: '–' }),
  gaps: el('b', { text: '0' }),
  dropped: el('b', { text: '0' }),
  price: el('b', { text: '–' }),
  msg: el('span', { class: 'muted hide-m' }),
};
const statusbar = el(
  'div',
  { class: 'statusbar', role: 'status' },
  sb.badge,
  sb.fresh,
  el('span', {}, 'Обновл. ', sb.last),
  el('span', {}, 'Цена ', sb.price),
  el('span', {}, 'Задержка ', sb.lat),
  el('span', { class: 'hide-m' }, 'RTT ', sb.rtt),
  el('span', {}, 'Сделок ', sb.trades),
  el('span', {}, 'Цен в стакане ', sb.depth),
  el('span', { class: 'hide-m' }, 'Разрывы/ресинхр. ', sb.gaps),
  el('span', { class: 'hide-m' }, 'Отброшено ', sb.dropped),
  el('span', { class: 'hide-m' }, 'Источник ', sb.src),
  sb.hist,
  sb.msg,
);
const noteBar = el('div', { class: 'note', style: 'display:none' });

// ---------- tabs ----------
const nav = el('nav', { class: 'tabs', role: 'tablist' });
const main = el('main');
app.append(header, statusbar, noteBar, nav, main);

const tabs: Tab[] = [createChartTab(), createHeatmapTab(), createDomTab(), createFootprintTab(), createProfileTab(), createSignalsTab(), createLevelsTab(), createPaperTab(), createAlertsTab(), createSourcesTab()];
let active: Tab | null = null;
for (const t of tabs) {
  t.root.classList.add('tab');
  main.append(t.root);
  const b = el('button', { role: 'tab', 'data-tab': t.id, text: t.title });
  b.onclick = () => activate(t.id);
  nav.append(b);
}
function activate(id: string): void {
  const t = tabs.find((x) => x.id === id) ?? tabs[0];
  if (active === t) return;
  active?.hide();
  active?.root.classList.remove('on');
  active = t;
  t.root.classList.add('on');
  for (const b of nav.querySelectorAll('button')) b.classList.toggle('on', b.getAttribute('data-tab') === t.id);
  savePref('tab', t.id);
  location.hash = t.id;
  t.show();
}

// ---------- network worker ----------
const worker = new Worker(new URL('./net.worker.ts', import.meta.url), { type: 'module' });
const wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
worker.postMessage({ op: 'init', url: wsUrl });

worker.onmessage = (e: MessageEvent) => {
  const { batch, stats } = e.data as { batch: { ch: string; k?: string; d: unknown }[]; stats: { received: number; dropped: number } };
  store.net.received = stats.received;
  store.net.droppedClient = stats.dropped;
  const touched = new Set<string>();
  for (const m of batch) {
    if (m.k && m.k !== store.key && m.ch !== 'net' && m.ch !== 'rtt') continue;
    switch (m.ch) {
      case 'net': {
        const prev = store.net.state;
        store.net.state = (m.d as { state: string }).state;
        if (store.net.state === 'open' && prev !== 'open') void checkBuild();
        if (prev === 'open' && store.net.state !== 'open') {
          // the browser lost its connection to the server: raise it through the alert rules like any feed event
          const t = Date.now();
          alerts.onEvent({ id: `client-net-${t}`, t, kind: 'feed', title: 'Связь браузера с сервером потеряна — переподключение', price: store.lastTrade?.price ?? NaN, confidence: 100, explain: 'Соединение с сервером OrderFlow прервалось; данные на экране заморожены до переподключения.', source: store.source, symbol: store.symbol }, true);
        }
        touched.add('net');
        break;
      }
      case 'rtt': {
        const d = m.d as { rtt: number; offset: number };
        store.net.rtt = d.rtt;
        store.net.offset = d.offset;
        touched.add('net');
        break;
      }
      case 'subscribed':
        store.meta = m.d as InstrumentMeta;
        touched.add('meta');
        break;
      case 'error':
        sb.msg.textContent = (m.d as { message: string }).message;
        break;
      case 'status':
        store.status = m.d as StatusX;
        touched.add('status');
        break;
      case 'book':
        store.book = m.d as BookMsg;
        touched.add('book');
        break;
      case 'trades': {
        const list = (m.d as [number, number, number, 1 | -1, number][]).map(([t, price, qty, side, id]): Trade => ({ t, price, qty, side, id: id || undefined }));
        store.addTrades(list);
        touched.add('trades');
        break;
      }
      case 'heat':
        store.addHeat(m.d as HeatColumn);
        touched.add('heat');
        break;
      case 'event': {
        const ev = m.d as MarketEvent;
        const isNew = !store.events.has(ev.id);
        store.upsertEvent(ev);
        alerts.onEvent(ev, isNew);
        touched.add('events');
        break;
      }
      case 'large':
        store.large = m.d as typeof store.large;
        touched.add('large');
        break;
      case 'clusters':
        store.clusters = m.d as typeof store.clusters;
        touched.add('clusters');
        break;
      case 'ice':
        store.ice = m.d as typeof store.ice;
        touched.add('ice');
        break;
      case 'deriv':
        store.deriv = m.d as typeof store.deriv;
        touched.add('deriv');
        break;
      case 'liq':
        store.liqs.push(m.d as (typeof store.liqs)[number]);
        if (store.liqs.length > 500) store.liqs.shift();
        touched.add('liq');
        break;
      case 'setup': {
        const s = m.d as import('../core/levelEngine/setupEngine.js').Setup;
        if (s.symbol === store.symbol && !store.setups.some((x) => x.id === s.id)) {
          store.setups.push(s);
          store.emit('setups');
        }
        break;
      }
      case 'alert': {
        const a = m.d as { event: MarketEvent; sound: boolean; notify: boolean; ruleId: number; t: number };
        alerts.onServerAlert(a);
        break;
      }
      case 'gap':
        // server dropped messages for this slow client: reload the missing trades window
        void loadTrades((m.d as { from: number }).from);
        break;
      case 'backfill':
        void loadTrades(store.now() - 2 * 3600_000);
        break;
    }
  }
  for (const t of touched) store.emit(t as Parameters<typeof store.emit>[0]);
  worker.postMessage({ op: 'ack' });
};

// ---------- instrument / timeframe ----------
let instruments: InstrumentMeta[] = [];
async function loadInstruments(): Promise<void> {
  try {
    instruments = await api<InstrumentMeta[]>('/api/instruments', { source: store.source });
    symbolList.replaceChildren(...instruments.map((i) => el('option', { value: i.symbol })));
    sb.msg.textContent = '';
  } catch (e) {
    sb.msg.textContent = 'Instrument list unavailable: ' + (e as Error).message;
  }
}

async function loadTrades(from: number): Promise<void> {
  try {
    const r = await api<{ trades: [number, number, number, 1 | -1, number][] }>('/api/trades', { source: store.source, symbol: store.symbol, from: Math.round(from), limit: 150_000 });
    store.prependTrades(
      r.trades.map(([t, price, qty, side, id]) => ({ t, price, qty, side, id: id || undefined })),
      from,
    );
    store.emit('trades');
    store.emit('history');
  } catch (e) {
    console.warn('trades history', e);
  }
}

async function loadHistory(): Promise<void> {
  const key = store.key;
  const now = store.now();
  const [ev, heat] = await Promise.allSettled([
    api<MarketEvent[]>('/api/events', { source: store.source, symbol: store.symbol, from: now - 24 * 3600_000 }),
    api<{ cols: HeatColumn[] }>('/api/heatmap', { source: store.source, symbol: store.symbol, from: now - 15 * 60_000, maxCols: 1200 }),
  ]);
  if (key !== store.key) return;
  if (ev.status === 'fulfilled') for (const e of ev.value) store.upsertEvent(e);
  if (heat.status === 'fulfilled') store.setHeatHistory(heat.value.cols, now - 15 * 60_000);
  await loadTrades(now - 2 * 3600_000);
  // fill the seam between the REST history and the first live columns once the recorder has flushed
  setTimeout(() => {
    if (key !== store.key) return;
    void api<{ cols: HeatColumn[] }>('/api/heatmap', { source: store.source, symbol: store.symbol, from: now - 60_000, maxCols: 600 }).then((r) => {
      if (key !== store.key) return;
      store.setHeatHistory(r.cols, store.heatFrom || now - 60_000);
      store.emit('heat');
    }, () => {});
  }, 3000);
  store.emit('events');
  store.emit('heat');
  store.emit('history');
}

function selectSymbol(source: SourceId, symbol: string): void {
  symbol = symbol.trim().toUpperCase();
  if (!/^[A-Z0-9]{2,30}$/.test(symbol)) return;
  if (instruments.length && !instruments.some((i) => i.symbol === symbol)) {
    sb.msg.textContent = `${symbol} нет в списке инструментов ${source}`;
    return;
  }
  store.source = source;
  store.symbol = symbol;
  symbolInput.value = symbol;
  savePref('instrument', { source, symbol });
  store.reset();
  const meta = instruments.find((i) => i.symbol === symbol);
  if (meta) store.meta = meta;
  updateNote();
  worker.postMessage({ op: 'sub', source, symbol });
  store.emit('meta');
  void loadHistory();
}

// the Levels tab opens a watchlist coin on the chart
(window as unknown as { oftOpen: (s: string, y: string) => void }).oftOpen = (source, symbol) => {
  if (source === store.source) selectSymbol(source as SourceId, symbol);
  location.hash = '#chart';
};

function setTf(tf: Timeframe): void {
  store.tf = tf;
  for (const b of tfGroup.querySelectorAll('button')) b.classList.toggle('on', b.getAttribute('data-tf') === tf);
  tfSelect.value = tf;
  ticksSel.style.display = tf === 'tick' ? '' : 'none';
  savePref('tf', tf);
  store.emit('tf');
}

function updateNote(): void {
  const m = store.meta;
  const notes: string[] = [];
  if (m?.note) notes.push(m.note);
  noteBar.textContent = notes.join(' ');
  noteBar.style.display = notes.length ? '' : 'none';
}
store.on('meta', updateNote);

sourceSel.onchange = async () => {
  store.source = sourceSel.value as SourceId;
  await loadInstruments();
  selectSymbol(store.source, instruments.some((i) => i.symbol === store.symbol) ? store.symbol : 'BTCUSDT');
};
symbolInput.onchange = () => selectSymbol(store.source, symbolInput.value);
symbolInput.onkeydown = (e) => {
  if (e.key === 'Enter') selectSymbol(store.source, symbolInput.value);
};
ticksSel.onchange = () => {
  store.ticksPerBar = +ticksSel.value;
  savePref('ticks', store.ticksPerBar);
  store.emit('tf');
};

// ---------- status bar rendering ----------
const STATE_LABEL: Record<string, string> = {
  connected: 'LIVE',
  reconnecting: 'Переподключение',
  connecting: 'Подключение',
  stale: 'Данные устарели',
  syncing: 'Синхронизация стакана',
  gap: 'Разрыв данных',
  disconnected: 'Отключено',
};
function renderStatus(): void {
  const s = store.status;
  const netDown = store.net.state !== 'open';
  const state = netDown ? (store.net.state === 'connecting' ? 'connecting' : 'reconnecting') : (s?.state ?? 'connecting');
  sb.badge.className = 'badge ' + state;
  sb.badge.textContent = netDown ? 'Сервер: ' + (STATE_LABEL[state] ?? state) : STATE_LABEL[state] ?? state;
  sb.badge.title = s?.gate === false && s.gateReason ? 'Новые сигналы заблокированы: ' + s.gateReason : '';
  const ps = s?.persist;
  // a past error followed by a successful write is not the current state
  const psErr = ps && ps.lastError && (ps.lastErrorAt ?? 0) > ps.lastOkAt ? ps.lastError : '';
  sb.hist.textContent = !ps ? '' : !ps.enabled ? '⚠ История не записывается' : psErr ? '⚠ Ошибка записи истории' : `История: запись ок · очередь ${ps.queued}`;
  sb.hist.className = 'hist ' + (!ps || (ps.enabled && !psErr) ? 'muted' : 'warn');
  sb.hist.title = psErr || (ps ? `Последняя успешная запись: ${ps.lastOkAt ? new Date(ps.lastOkAt).toISOString() : '—'}; несохранённый хвост ≈ ${Math.round(ps.oldestUnsavedMs / 1000)} с` : '');
  const age = s?.lastUpdate ? Date.now() - s.lastUpdate : NaN;
  sb.last.textContent = s?.lastUpdate ? ago(age) + ' назад' : '–';
  sb.fresh.style.background = !isFinite(age) ? '#8a93a6' : age < 1500 ? '#2ecc71' : age < 5000 ? '#ffb300' : '#ef5350';
  sb.lat.textContent = s && isFinite(s.latencyMs) ? s.latencyMs + ' мс' : '–';
  sb.rtt.textContent = isFinite(store.net.rtt) ? store.net.rtt + ' мс' : '–';
  sb.trades.textContent = s ? String(s.trades) : '0';
  sb.depth.textContent = s ? String(s.depthLevels) : '0';
  sb.gaps.textContent = s ? `${s.gaps}/${s.resyncs}` : '0/0';
  sb.dropped.textContent = `${s?.dropped ?? 0}/${store.net.droppedClient}`;
  sb.src.textContent = store.meta ? `${store.meta.source} ${store.meta.symbol}` : store.key;
  const lt = store.lastTrade;
  sb.price.textContent = lt ? fmtP(lt.price, store.meta?.pricePrecision ?? 2) : '–';
  if (s?.message && s.state !== 'connected') sb.msg.textContent = s.message;
  else if (s?.state === 'connected') sb.msg.textContent = store.deriv.mark ? `Mark ${store.deriv.mark} · Funding ${((store.deriv.funding ?? 0) * 100).toFixed(4)}% · OI ${fmtQ(store.deriv.oi ?? NaN)}` : '';
}
store.on('status', renderStatus);
store.on('net', renderStatus);
setInterval(renderStatus, 1000);

// ---------- boot ----------
const inst = loadPrefRaw<{ source: SourceId; symbol: string }>('instrument', { source: 'binance-futures', symbol: 'BTCUSDT' });
store.source = inst.source;
sourceSel.value = inst.source;
store.ticksPerBar = loadPrefRaw('ticks', 100);
ticksSel.value = String(store.ticksPerBar);
setTf(loadPrefRaw<Timeframe>('tf', '1m'));
activate((location.hash.slice(1) || loadPrefRaw('tab', 'chart')) as string);
void loadInstruments().then(() => selectSymbol(inst.source, inst.symbol));
if ('serviceWorker' in navigator) void navigator.serviceWorker.register('/sw.js').catch(() => {});

// ---------- new deploy -> reload the tab (an open page must not keep running an old UI) ----------
let knownBuild = '';
async function checkBuild(): Promise<void> {
  try {
    const h = (await (await fetch('/api/health', { cache: 'no-store' })).json()) as { build?: string };
    if (!h.build) return;
    if (!knownBuild) knownBuild = h.build;
    else if (h.build !== knownBuild) {
      noteBar.textContent = 'На сервере новая версия — обновляю страницу…';
      noteBar.style.display = '';
      setTimeout(() => location.reload(), 1500);
    }
  } catch {
    /* server unreachable: the reconnect will check again */
  }
}
void checkBuild();
setInterval(() => void checkBuild(), 5 * 60_000);
