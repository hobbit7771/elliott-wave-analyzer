// Volume Profile (exact, from recorded aggressive trades) and TPO (from exchange 30m candles).
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, fitCanvas, Painter, fmtQ, fmtP, fmtDateTime } from '../util.js';
import { volumeProfile, tpoProfile, type TpoProfile, type VolumeProfile } from '../../core/footprint.js';
import { decimalsOf } from '../../core/precision.js';
import type { Candle } from '../../core/types.js';

export function createProfileTab(): Tab {
  const root = el('section', { id: 'tab-profile', role: 'tabpanel' });
  const rangeSel = el('select', { 'aria-label': 'Диапазон профиля объёма' });
  for (const [v, l] of [['session', 'Сессия (UTC-день)'], ['3600000', 'Последний 1 ч'], ['14400000', 'Последние 4 ч'], ['all', 'Всё загруженное']]) rangeSel.append(el('option', { value: v, text: l }));
  const rowSel = el('select', { 'aria-label': 'Размер строки' });
  rowSel.append(el('option', { value: '0', text: 'авто' }));
  for (const n of [1, 5, 10, 25, 50, 100, 250, 500, 1000]) rowSel.append(el('option', { value: String(n), text: `${n} tick` }));
  const daySel = el('select', { 'aria-label': 'TPO session' });
  const vpInfo = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, el('label', {}, 'VP', rangeSel), el('label', {}, 'Строк', rowSel), el('label', {}, 'TPO: сессия (UTC-сутки)', daySel), vpInfo));
  const split = el('div', { class: 'split', style: 'flex-wrap:wrap' });
  const vpBox = el('div', { class: 'fill', style: 'min-width:280px;min-height:260px' });
  const tpoBox = el('div', { class: 'fill', style: 'min-width:280px;min-height:260px;border-left:1px solid var(--line)' });
  const vpCanvas = el('canvas');
  const tpoCanvas = el('canvas');
  const vpEmpty = el('div', { class: 'empty' });
  const tpoEmpty = el('div', { class: 'empty' });
  vpBox.append(vpCanvas, vpEmpty);
  tpoBox.append(tpoCanvas, tpoEmpty);
  split.append(vpBox, tpoBox);
  root.append(split);

  let tpoCandles: Candle[] = [];
  let tpoErr = '';
  const painter = new Painter(draw, 500);
  const tick = () => store.meta?.tickSize ?? 0.01;

  function stepFor(range: number): number {
    const m = +rowSel.value;
    if (m) return +(m * tick()).toFixed(decimalsOf(tick()));
    const raw = Math.max(tick(), range / 60);
    const nice = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000];
    const k = nice.find((n) => n * tick() >= raw) ?? nice[nice.length - 1];
    return +(k * tick()).toFixed(decimalsOf(tick()));
  }

  function vpTrades() {
    const now = store.now();
    const v = rangeSel.value;
    const from = v === 'session' ? Math.floor(now / 86_400_000) * 86_400_000 : v === 'all' ? 0 : now - +v;
    return store.trades.filter((t) => t.t >= from);
  }

  function draw(): void {
    drawVp();
    drawTpo();
  }

  function drawVp(): void {
    const { ctx, w, h } = fitCanvas(vpCanvas);
    ctx.fillStyle = '#0b0e14';
    ctx.fillRect(0, 0, w, h);
    const tr = vpTrades();
    if (!tr.length) {
      vpEmpty.textContent = 'В этом диапазоне ещё нет записанных сделок.';
      vpInfo.textContent = '';
      return;
    }
    vpEmpty.textContent = '';
    let lo = Infinity;
    let hi = -Infinity;
    for (const t of tr) {
      lo = Math.min(lo, t.price);
      hi = Math.max(hi, t.price);
    }
    const step = stepFor(hi - lo);
    const vp: VolumeProfile = volumeProfile(tr, step);
    const dec = Math.max(store.meta?.pricePrecision ?? 2, decimalsOf(step));
    vpInfo.textContent = `VP из ${tr.length} сообщений aggTrade ${fmtDateTime(tr[0].t)} → ${fmtDateTime(tr[tr.length - 1].t)} · POC ${fmtP(vp.poc, dec)} VAH ${fmtP(vp.vah, dec)} VAL ${fmtP(vp.val, dec)}`;
    const rows = vp.rows;
    const axisW = 70;
    const rh = Math.max(1, Math.min(18, (h - 20) / rows.length));
    const maxV = Math.max(...rows.map((r) => r.vol), 1e-12);
    const W = w - axisW - 8;
    ctx.font = '10px sans-serif';
    ctx.textBaseline = 'middle';
    rows.forEach((r, i) => {
      const y = h - 10 - (i + 1) * rh;
      const inVA = r.price >= vp.val && r.price <= vp.vah;
      const bw = (r.buy / maxV) * W;
      const sw = (r.sell / maxV) * W;
      ctx.fillStyle = inVA ? '#ef5350cc' : '#ef535066';
      ctx.fillRect(axisW, y, sw, Math.max(1, rh - 1));
      ctx.fillStyle = inVA ? '#26a69acc' : '#26a69a66';
      ctx.fillRect(axisW + sw, y, bw, Math.max(1, rh - 1));
      if (r.price === vp.poc) {
        ctx.strokeStyle = '#ffd54f';
        ctx.strokeRect(axisW, y, W, Math.max(1, rh - 1));
      }
      if (rh >= 9 || i % Math.ceil(10 / rh) === 0) {
        ctx.fillStyle = '#aab2c5';
        ctx.fillText(fmtP(r.price, dec), 2, y + rh / 2);
      }
    });
    const yOf = (price: number) => h - 10 - ((price - rows[0].price) / step + 0.5) * rh;
    const line = (price: number, color: string, label: string) => {
      const y = yOf(price);
      ctx.strokeStyle = color;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(axisW, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.fillText(label, w - 40, y - 6);
    };
    line(vp.vah, '#4ea1ff', 'VAH');
    line(vp.val, '#4ea1ff', 'VAL');
    line(vp.poc, '#ffd54f', 'POC');
    for (const p of vp.hvn) {
      ctx.fillStyle = '#ffd54f';
      ctx.fillText('HVN', axisW + 4, yOf(p));
    }
    for (const p of vp.lvn) {
      ctx.fillStyle = '#90a4ae';
      ctx.fillText('LVN', axisW + 4, yOf(p));
    }
    ctx.fillStyle = '#8a93a6';
    ctx.fillText(`total ${fmtQ(vp.total)}`, axisW + 4, 8);
  }

  function drawTpo(): void {
    const { ctx, w, h } = fitCanvas(tpoCanvas);
    ctx.fillStyle = '#0b0e14';
    ctx.fillRect(0, 0, w, h);
    const day = +daySel.value;
    const periods = tpoCandles.filter((c) => Math.floor(c.t / 86_400_000) === day);
    if (!periods.length) {
      tpoEmpty.textContent = tpoErr || 'Загрузка 30-минутных свечей…';
      return;
    }
    tpoEmpty.textContent = '';
    const lo = Math.min(...periods.map((c) => c.l));
    const hi = Math.max(...periods.map((c) => c.h));
    const step = stepFor(hi - lo);
    const t: TpoProfile = tpoProfile(periods, step);
    const dec = Math.max(store.meta?.pricePrecision ?? 2, decimalsOf(step));
    const rows = t.rows;
    const axisW = 70;
    const rh = Math.max(4, Math.min(16, (h - 30) / rows.length));
    const cw = Math.max(5, Math.min(10, (w - axisW - 10) / Math.max(1, t.periods)));
    ctx.font = `${Math.min(11, rh)}px ui-monospace, Menlo, monospace`;
    ctx.textBaseline = 'middle';
    const y0 = h - 20;
    rows.forEach((r, i) => {
      const y = y0 - (i + 1) * rh;
      const inVA = r.price >= t.val && r.price <= t.vah;
      ctx.fillStyle = r.price === t.poc ? '#ffd54f' : inVA ? '#4ea1ff' : '#8a93a6';
      for (let k = 0; k < r.letters.length; k++) ctx.fillText(r.letters[k], axisW + k * cw, y + rh / 2);
      if (rh >= 9 || i % Math.ceil(10 / rh) === 0) {
        ctx.fillStyle = '#aab2c5';
        ctx.fillText(fmtP(r.price, dec), 2, y + rh / 2);
      }
    });
    const yOf = (price: number) => y0 - ((price - rows[0].price) / step + 0.5) * rh;
    for (const [price, label, color] of [[t.ibHigh, 'IB high', '#ab47bc'], [t.ibLow, 'IB low', '#ab47bc']] as const) {
      const y = yOf(price);
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(axisW, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.fillText(label, w - 48, y - 6);
    }
    ctx.fillStyle = '#8a93a6';
    ctx.fillText(`TPO ≈ ${new Date(day * 86_400_000).toISOString().slice(0, 10)} UTC · периодов 30м: ${t.periods} · POC ${fmtP(t.poc, dec)} VA ${fmtP(t.val, dec)}–${fmtP(t.vah, dec)}`, 4, 10);
    ctx.fillStyle = '#8a93a6';
    ctx.fillText('Приближение: каждый 30м период заполняет весь диапазон high–low свечи (без тиков внутри периода). Сессия = сутки UTC, без перехода на летнее время.', 4, h - 4);
  }

  async function loadTpo(): Promise<void> {
    tpoErr = '';
    try {
      const r = await api<{ candles: Candle[] }>('/api/klines', { source: store.source, symbol: store.symbol, tf: '30m', limit: 48 * 5 });
      tpoCandles = r.candles;
      const days = [...new Set(tpoCandles.map((c) => Math.floor(c.t / 86_400_000)))].sort((a, b) => b - a);
      const prev = daySel.value;
      daySel.replaceChildren(...days.map((d) => el('option', { value: String(d), text: new Date(d * 86_400_000).toISOString().slice(0, 10) })));
      if (prev && days.includes(+prev)) daySel.value = prev;
    } catch (e) {
      tpoErr = 'TPO unavailable: ' + (e as Error).message;
    }
    painter.mark();
  }

  for (const s of [rangeSel, rowSel, daySel]) s.addEventListener('change', () => painter.mark());
  store.on('trades', () => painter.mark());
  store.on('history', () => painter.mark());
  store.on('reset', () => {
    tpoCandles = [];
    if (painter.visible) void loadTpo();
  });
  new ResizeObserver(() => painter.mark()).observe(split);
  let tpoTimer = 0;
  return {
    id: 'profile',
    title: 'Profile/TPO',
    root,
    show() {
      painter.show();
      void loadTpo();
      tpoTimer = window.setInterval(() => void loadTpo(), 60_000);
    },
    hide() {
      painter.hide();
      clearInterval(tpoTimer);
    },
  };
}
