// DOM (price ladder): real bids/asks from the server's local book, executed volume per price,
// delta, volume, imbalance, large levels, probable icebergs, absorption and activity highlighting.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, fitCanvas, gestures, Painter, fmtQ, fmtP, fmtTime, loadPref, savePref } from '../util.js';
import { decimalsOf } from '../../core/precision.js';

interface Prefs {
  group: number;
  levels: number;
  largeMin: number;
  periodMs: number;
  scheme: 'classic' | 'ocean' | 'mono';
  autoCenter: boolean;
}
const DEF: Prefs = { group: 1, levels: 40, largeMin: 0, periodMs: 300_000, scheme: 'classic', autoCenter: true };
const SCHEMES = {
  classic: { bid: [38, 166, 154], ask: [239, 83, 80], text: '#d6dbe6', price: '#1a2130', hl: '#3a3320' },
  ocean: { bid: [66, 165, 245], ask: [255, 152, 0], text: '#e3f2fd', price: '#10263a', hl: '#2d3a20' },
  mono: { bid: [200, 200, 200], ask: [120, 120, 120], text: '#eeeeee', price: '#202020', hl: '#333' },
};

export function createDomTab(): Tab {
  const root = el('section', { id: 'tab-dom', role: 'tabpanel' });
  const p = loadPref<Prefs>('domPrefs', DEF);
  const save = () => savePref('domPrefs', p);

  const groupSel = el('select', { 'aria-label': 'Grouping' });
  for (const g of [1, 2, 5, 10, 25, 50, 100, 250, 1000]) groupSel.append(el('option', { value: String(g), text: `${g} tick${g > 1 ? 's' : ''}` }));
  groupSel.value = String(p.group);
  const levelsSel = el('select', { 'aria-label': 'Levels each side' });
  for (const n of [10, 20, 40, 60, 100, 200]) levelsSel.append(el('option', { value: String(n), text: `±${n}` }));
  levelsSel.value = String(p.levels);
  const largeIn = el('input', { type: 'number', min: '0', step: 'any', value: String(p.largeMin), title: 'Highlight levels at or above this size (0 = use the detector’s dynamic threshold)' });
  const refillIn = el('input', { type: 'number', min: '1', max: '50', step: '1', value: '3', title: 'Minimum replenishments for Probable Iceberg (server detector setting)' });
  const periodSel = el('select', { 'aria-label': 'Analysis period' });
  for (const [l, ms] of [['1m', 60_000], ['5m', 300_000], ['15m', 900_000], ['1h', 3600_000], ['loaded', 0]] as [string, number][]) periodSel.append(el('option', { value: String(ms), text: l }));
  periodSel.value = String(p.periodMs);
  const schemeSel = el('select', { 'aria-label': 'Color scheme' });
  for (const s of Object.keys(SCHEMES)) schemeSel.append(el('option', { value: s, text: s }));
  schemeSel.value = p.scheme;
  const centerBtn = el('button', { text: 'Center', class: p.autoCenter ? 'on' : '' });
  root.append(
    el('div', { class: 'toolbar' }, el('label', {}, 'Group', groupSel), el('label', {}, 'Levels', levelsSel), el('label', {}, 'Large ≥', largeIn), el('label', {}, 'Min refills', refillIn), el('label', {}, 'Period', periodSel), el('label', {}, 'Colors', schemeSel), centerBtn),
  );
  const cards = el('div', { class: 'cards pad' });
  const card = (k: string) => {
    const v = el('div', { class: 'v', text: '–' });
    cards.append(el('div', { class: 'card' }, el('div', { class: 'k', text: k }), v));
    return v;
  };
  const cLast = card('Last trade');
  const cSpread = card('Spread');
  const cMicro = card('Microprice / mid');
  const cObi = card('Book imbalance (top 10)');
  const cOfi = card('OFI 10s');
  const cSpeed = card('Speed (10s)');
  const cCvd = card('Session CVD');
  const cThr = card('Large threshold bid/ask');
  root.append(cards);
  const fill = el('div', { class: 'fill' });
  const canvas = el('canvas');
  const empty = el('div', { class: 'empty' });
  fill.append(canvas, empty);
  root.append(fill);

  let offsetRows = 0;
  let execCache: { key: string; buy: Map<number, number>; sell: Map<number, number>; flash: Map<number, number> } | null = null;
  const painter = new Painter(draw, 100);
  const tick = () => store.meta?.tickSize ?? 0.01;
  const g = () => tick() * p.group;
  const dec = () => Math.max(store.meta?.pricePrecision ?? 2, decimalsOf(g()));

  function execAgg(): NonNullable<typeof execCache> {
    const now = store.now();
    const from = p.periodMs ? now - p.periodMs : 0;
    const key = `${store.tradeSeq}|${p.group}|${p.periodMs}|${Math.floor(now / 1000)}`;
    if (execCache?.key === key) return execCache;
    const buy = new Map<number, number>();
    const sell = new Map<number, number>();
    const flash = new Map<number, number>();
    const gs = g();
    const tr = store.trades;
    for (let i = tr.length - 1; i >= 0; i--) {
      const t = tr[i];
      if (t.t < from) break;
      const r = Math.floor(t.price / gs + 1e-9);
      const m = t.side === 1 ? buy : sell;
      m.set(r, (m.get(r) ?? 0) + t.qty);
      if (now - t.t < 2000) flash.set(r, Math.max(flash.get(r) ?? 0, t.t));
    }
    execCache = { key, buy, sell, flash };
    return execCache;
  }

  function draw(): void {
    const { ctx, w, h } = fitCanvas(canvas);
    const S = SCHEMES[p.scheme];
    ctx.fillStyle = '#0b0e14';
    ctx.fillRect(0, 0, w, h);
    const b = store.book;
    renderCards();
    if (!b || !b.bids.length || !b.asks.length) {
      empty.textContent = store.status?.state === 'connected' ? 'Waiting for book…' : `Order book not available (${store.status?.state ?? 'connecting'}).`;
      return;
    }
    empty.textContent = '';
    const gs = g();
    const bidRows = new Map<number, number>();
    const askRows = new Map<number, number>();
    for (const [pr, q] of b.bids) {
      const r = Math.floor(pr / gs + 1e-9);
      bidRows.set(r, (bidRows.get(r) ?? 0) + q);
    }
    for (const [pr, q] of b.asks) {
      const r = Math.ceil(pr / gs - 1e-9);
      askRows.set(r, (askRows.get(r) ?? 0) + q);
    }
    const bestBidR = Math.floor(b.bids[0][0] / gs + 1e-9);
    const bestAskR = Math.ceil(b.asks[0][0] / gs - 1e-9);
    const rowH = w < 500 ? 17 : 19;
    const nRows = Math.floor(h / rowH);
    const midRow = Math.round((bestBidR + bestAskR) / 2);
    if (p.autoCenter) offsetRows = 0;
    // restrict to the configured number of levels each side
    const topR = Math.min(midRow + Math.floor(nRows / 2) + offsetRows, bestAskR + p.levels - 1);
    const botR = Math.max(topR - nRows + 1, bestBidR - p.levels + 1);
    const ex = execAgg();
    let maxQ = 0;
    let maxEx = 0;
    for (let r = botR; r <= topR; r++) {
      maxQ = Math.max(maxQ, bidRows.get(r) ?? 0, askRows.get(r) ?? 0);
      maxEx = Math.max(maxEx, (ex.buy.get(r) ?? 0) + (ex.sell.get(r) ?? 0));
    }
    const thr = store.large.thr;
    const largeB = p.largeMin > 0 ? p.largeMin : (thr?.bid ?? Infinity);
    const largeA = p.largeMin > 0 ? p.largeMin : (thr?.ask ?? Infinity);
    // markers: probable icebergs & absorption near price
    const iceRows = new Map<number, { c: number; hidden: number }>();
    for (const c of store.ice) if (c.eligible) iceRows.set(Math.floor(c.price / gs + 1e-9), { c: c.confidence, hidden: c.hidden });
    const now = store.now();
    const absRows = new Map<number, number>();
    for (const e of store.events.values()) {
      if (now - e.t > 120_000) continue;
      const r = Math.floor(e.price / gs + 1e-9);
      if (e.kind === 'absorption') absRows.set(r, Math.max(absRows.get(r) ?? 0, e.confidence));
      if (e.kind === 'iceberg' && e.status !== 'broken' && !iceRows.has(r)) iceRows.set(r, { c: e.confidence, hidden: Number(e.data?.estimatedHidden ?? 0) });
    }
    // columns
    const narrow = w < 560;
    const cols = narrow
      ? [
          { k: 'sell', w: 0.15, t: 'Sold' },
          { k: 'bid', w: 0.2, t: 'Bid' },
          { k: 'price', w: 0.23, t: 'Price' },
          { k: 'ask', w: 0.2, t: 'Ask' },
          { k: 'buy', w: 0.15, t: 'Bought' },
          { k: 'mark', w: 0.07, t: '' },
        ]
      : [
          { k: 'sell', w: 0.11, t: 'Sold @bid' },
          { k: 'bid', w: 0.15, t: 'Bid size' },
          { k: 'price', w: 0.14, t: 'Price' },
          { k: 'ask', w: 0.15, t: 'Ask size' },
          { k: 'buy', w: 0.11, t: 'Bought @ask' },
          { k: 'delta', w: 0.09, t: 'Delta' },
          { k: 'vol', w: 0.09, t: 'Volume' },
          { k: 'imb', w: 0.06, t: 'Imb' },
          { k: 'mark', w: 0.1, t: 'Signals' },
        ];
    let x = 0;
    const xs = cols.map((c) => {
      const o = { ...c, x, px: c.w * w };
      x += c.w * w;
      return o;
    });
    ctx.font = `${narrow ? 11 : 12}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.textBaseline = 'middle';
    const headerH = rowH;
    ctx.fillStyle = '#121722';
    ctx.fillRect(0, 0, w, headerH);
    ctx.fillStyle = '#8a93a6';
    for (const c of xs) ctx.fillText(c.t, c.x + 4, headerH / 2);
    const rgba = (c: number[], a: number) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
    for (let i = 0; i < nRows - 1; i++) {
      const r = topR - i;
      if (r < botR) break;
      const y = headerH + i * rowH;
      const price = r * gs;
      const bq = r <= bestBidR ? (bidRows.get(r) ?? 0) : 0;
      const aq = r >= bestAskR ? (askRows.get(r) ?? 0) : 0;
      const bu = ex.buy.get(r) ?? 0;
      const se = ex.sell.get(r) ?? 0;
      const flash = ex.flash.get(r);
      if (flash) {
        ctx.fillStyle = S.hl;
        ctx.globalAlpha = Math.max(0.2, 1 - (now - flash) / 2000);
        ctx.fillRect(0, y, w, rowH);
        ctx.globalAlpha = 1;
      }
      for (const c of xs) {
        const cx = c.x;
        const cw = c.px;
        let txt = '';
        let color = S.text;
        switch (c.k) {
          case 'bid':
            if (bq > 0) {
              ctx.fillStyle = rgba(S.bid, 0.25);
              ctx.fillRect(cx + cw - (bq / maxQ) * cw, y + 1, (bq / maxQ) * cw, rowH - 2);
              txt = fmtQ(bq);
              if (bq >= largeB) {
                ctx.strokeStyle = rgba(S.bid, 1);
                ctx.lineWidth = 2;
                ctx.strokeRect(cx + 1, y + 1, cw - 2, rowH - 2);
                ctx.lineWidth = 1;
              }
            }
            break;
          case 'ask':
            if (aq > 0) {
              ctx.fillStyle = rgba(S.ask, 0.25);
              ctx.fillRect(cx, y + 1, (aq / maxQ) * cw, rowH - 2);
              txt = fmtQ(aq);
              if (aq >= largeA) {
                ctx.strokeStyle = rgba(S.ask, 1);
                ctx.lineWidth = 2;
                ctx.strokeRect(cx + 1, y + 1, cw - 2, rowH - 2);
                ctx.lineWidth = 1;
              }
            }
            break;
          case 'price':
            ctx.fillStyle = r === bestBidR ? rgba(S.bid, 0.35) : r === bestAskR ? rgba(S.ask, 0.35) : S.price;
            ctx.fillRect(cx, y, cw, rowH);
            txt = fmtP(price, dec());
            if (store.lastTrade && Math.floor(store.lastTrade.price / gs + 1e-9) === r) color = '#fff';
            break;
          case 'sell':
            if (se > 0) {
              ctx.fillStyle = rgba(S.ask, 0.18);
              ctx.fillRect(cx + cw - (se / maxEx) * cw, y + 1, (se / maxEx) * cw, rowH - 2);
              txt = fmtQ(se);
            }
            break;
          case 'buy':
            if (bu > 0) {
              ctx.fillStyle = rgba(S.bid, 0.18);
              ctx.fillRect(cx, y + 1, (bu / maxEx) * cw, rowH - 2);
              txt = fmtQ(bu);
            }
            break;
          case 'delta':
            if (bu || se) {
              txt = fmtQ(bu - se);
              color = bu >= se ? rgba(S.bid, 1) : rgba(S.ask, 1);
            }
            break;
          case 'vol':
            if (bu || se) txt = fmtQ(bu + se);
            break;
          case 'imb':
            if (bu + se > 0) {
              const im = (bu - se) / (bu + se);
              ctx.fillStyle = im >= 0 ? rgba(S.bid, 0.6) : rgba(S.ask, 0.6);
              ctx.fillRect(cx + cw / 2, y + 3, (im * cw) / 2, rowH - 6);
            }
            break;
          case 'mark': {
            const ice = iceRows.get(r);
            const abs = absRows.get(r);
            const parts: string[] = [];
            if (ice) parts.push(`ICE ${ice.c}`);
            if (abs) parts.push(`ABS ${abs}`);
            if (parts.length) {
              txt = parts.join(' ');
              color = ice ? '#b388ff' : '#ff9800';
            }
            break;
          }
        }
        if (txt) {
          ctx.fillStyle = color;
          ctx.fillText(txt, cx + 4, y + rowH / 2 + 1);
        }
      }
      ctx.fillStyle = '#161c28';
      ctx.fillRect(0, y + rowH - 1, w, 1);
    }
  }

  function renderCards(): void {
    const s = store.book?.stats;
    const d = dec();
    const lt = store.lastTrade;
    cLast.textContent = lt ? `${fmtP(lt.price, store.meta?.pricePrecision ?? 2)} ${lt.side === 1 ? '▲' : '▼'} ${fmtQ(lt.qty)} ${fmtTime(lt.t)}` : '–';
    cLast.className = 'v ' + (lt ? (lt.side === 1 ? 'pos' : 'neg') : '');
    cSpread.textContent = s ? `${fmtP(s.spread, d)} (${s.bidQty ? fmtQ(s.bidQty) : '–'} × ${s.askQty ? fmtQ(s.askQty) : '–'})` : '–';
    cMicro.textContent = s ? `${fmtP(s.microprice, d + 1)} / ${fmtP(s.mid, d + 1)}` : '–';
    cObi.textContent = s ? `${(s.obi * 100).toFixed(0)}%` : '–';
    cObi.className = 'v ' + (s ? (s.obi >= 0 ? 'pos' : 'neg') : '');
    cOfi.textContent = s ? fmtQ(s.ofi) : '–';
    const now = store.now();
    let n = 0;
    let v = 0;
    for (let i = store.trades.length - 1; i >= 0 && now - store.trades[i].t <= 10_000; i--) {
      n++;
      v += store.trades[i].qty;
    }
    cSpeed.textContent = `${(n / 10).toFixed(1)} tr/s · ${fmtQ(v / 10)}/s`;
    cCvd.textContent = store.status?.cvd !== undefined ? fmtQ(store.status.cvd) : '–';
    const t = store.large.thr;
    cThr.textContent = t?.warm ? `${fmtQ(t.bid)} / ${fmtQ(t.ask)}` : t ? `warming (${t.samples})` : '–';
  }

  gestures(canvas, {
    drag: (_dx, dy) => {
      p.autoCenter = false;
      centerBtn.classList.remove('on');
      offsetRows += dy / 18;
      painter.mark();
    },
    zoom: (f) => {
      p.autoCenter = false;
      centerBtn.classList.remove('on');
      offsetRows += f > 1 ? -3 : 3;
      painter.mark();
    },
  });
  centerBtn.onclick = () => {
    p.autoCenter = !p.autoCenter;
    offsetRows = 0;
    centerBtn.classList.toggle('on', p.autoCenter);
    save();
    painter.mark();
  };
  groupSel.onchange = () => {
    p.group = +groupSel.value;
    save();
    painter.mark();
  };
  levelsSel.onchange = () => {
    p.levels = +levelsSel.value;
    save();
    painter.mark();
  };
  largeIn.onchange = () => {
    p.largeMin = Math.max(0, +largeIn.value || 0);
    save();
    painter.mark();
  };
  periodSel.onchange = () => {
    p.periodMs = +periodSel.value;
    save();
    painter.mark();
  };
  schemeSel.onchange = () => {
    p.scheme = schemeSel.value as Prefs['scheme'];
    save();
    painter.mark();
  };
  refillIn.onchange = async () => {
    const v = Math.max(1, Math.round(+refillIn.value || 3));
    try {
      await api('/api/config', { source: store.source, symbol: store.symbol }, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ iceberg: { minRefills: v } }) });
    } catch (e) {
      alert('Could not update detector: ' + (e as Error).message);
    }
  };
  async function loadCfg(): Promise<void> {
    try {
      const r = await api<{ config: { iceberg: { minRefills: number } } }>('/api/config', { source: store.source, symbol: store.symbol });
      refillIn.value = String(r.config.iceberg.minRefills);
    } catch {
      /* keep default */
    }
  }

  for (const t of ['book', 'trades', 'ice', 'large', 'events'] as const) store.on(t, () => painter.mark());
  store.on('reset', () => {
    execCache = null;
    void loadCfg();
    painter.mark();
  });
  new ResizeObserver(() => painter.mark()).observe(fill);
  return {
    id: 'dom',
    title: 'DOM',
    root,
    show() {
      painter.show();
      void loadCfg();
    },
    hide() {
      painter.hide();
    },
  };
}
