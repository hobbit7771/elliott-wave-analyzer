// Footprint: bid x ask per price per bar built from real aggressive trades (recorded + live),
// with delta, imbalances, stacked imbalances, POC, HVN/LVN, unfinished auctions and absorption.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { el, fitCanvas, gestures, Painter, fmtQ, fmtP, fmtDateTime, loadPref, savePref } from '../util.js';
import { FootprintBuilder, analyzeBar, finishProfile, rowOf, type FootprintAnalysis, type FootprintBar, type ProfileRow } from '../../core/footprint.js';
import { decimalsOf } from '../../core/precision.js';
import { TF_MS } from '../../core/candles.js';

interface Prefs {
  stepTicks: number; // 0 = auto
  ratio: number;
  minVol: number;
  stack: number;
  mode: 'bidask' | 'delta' | 'volume';
  colW: number;
  rowH: number;
}
const DEF: Prefs = { stepTicks: 0, ratio: 3, minVol: 0, stack: 3, mode: 'bidask', colW: 110, rowH: 16 };

export function createFootprintTab(): Tab {
  const root = el('section', { id: 'tab-footprint', role: 'tabpanel' });
  const p = loadPref<Prefs>('fpPrefs', DEF);
  const save = () => savePref('fpPrefs', p);
  const stepSel = el('select', { 'aria-label': 'Row size' });
  stepSel.append(el('option', { value: '0', text: 'auto' }));
  for (const n of [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]) stepSel.append(el('option', { value: String(n), text: `${n} tick` }));
  stepSel.value = String(p.stepTicks);
  const ratioIn = el('input', { type: 'number', min: '1.5', max: '10', step: '0.5', value: String(p.ratio), title: 'Diagonal imbalance ratio' });
  const minVolIn = el('input', { type: 'number', min: '0', step: 'any', value: String(p.minVol), title: 'Minimum volume for an imbalance cell' });
  const stackIn = el('input', { type: 'number', min: '2', max: '10', step: '1', value: String(p.stack), title: 'Consecutive imbalances for a stacked imbalance' });
  const modeSel = el('select', { 'aria-label': 'Cell content' });
  for (const [v, l] of [['bidask', 'Bid × Ask'], ['delta', 'Delta'], ['volume', 'Volume']]) modeSel.append(el('option', { value: v, text: l }));
  modeSel.value = p.mode;
  const liveBtn = el('button', { text: 'Live', class: 'on' });
  const cover = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, el('label', {}, 'Row', stepSel), el('label', {}, 'Imb ratio', ratioIn), el('label', {}, 'Imb min vol', minVolIn), el('label', {}, 'Stack', stackIn), el('label', {}, 'Cells', modeSel), liveBtn, cover));
  const fill = el('div', { class: 'fill' });
  const canvas = el('canvas');
  const tip = el('div', { class: 'tooltip' });
  const empty = el('div', { class: 'empty' });
  fill.append(canvas, tip, empty);
  root.append(fill);

  let fb: FootprintBuilder | null = null;
  let step = 0;
  let seq = 0;
  let builtKey = '';
  let scrollBars = 0; // bars scrolled back from the latest
  let centerPrice = NaN;
  let followPrice = true;
  const analysis = new Map<FootprintBar, { a: FootprintAnalysis; n: number }>();
  const painter = new Painter(draw, 120);
  const tick = () => store.meta?.tickSize ?? 0.01;
  const dec = () => Math.max(store.meta?.pricePrecision ?? 2, decimalsOf(step || tick()));

  function autoStep(): number {
    const tr = store.trades;
    if (!tr.length) return tick();
    // aim for ~20 rows across a typical bar range: estimate from price range per bar duration
    const barMs = store.tf === 'tick' ? 60_000 : TF_MS[store.tf as keyof typeof TF_MS];
    const ranges: number[] = [];
    let lo = Infinity;
    let hi = -Infinity;
    let b = Math.floor(tr[0].t / barMs);
    for (const t of tr) {
      const k = Math.floor(t.t / barMs);
      if (k !== b) {
        if (hi >= lo) ranges.push(hi - lo);
        b = k;
        lo = Infinity;
        hi = -Infinity;
      }
      lo = Math.min(lo, t.price);
      hi = Math.max(hi, t.price);
    }
    ranges.sort((a, c) => a - c);
    const med = ranges.length ? ranges[Math.floor(ranges.length / 2)] : hi - lo;
    const raw = Math.max(tick(), med / 20);
    const nice = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000];
    const m = nice.find((n) => n * tick() >= raw) ?? nice[nice.length - 1];
    return +(m * tick()).toFixed(decimalsOf(tick()));
  }

  function rebuild(): void {
    step = p.stepTicks ? +(p.stepTicks * tick()).toFixed(decimalsOf(tick())) : autoStep();
    fb = new FootprintBuilder(store.tf, step, 600, store.ticksPerBar);
    analysis.clear();
    for (const t of store.trades) fb.add(t);
    seq = store.tradeSeq;
    builtKey = `${store.key}|${store.tf}|${store.ticksPerBar}|${p.stepTicks}|${store.tradesFrom}`;
    cover.textContent = store.trades.length ? `Trades loaded since ${fmtDateTime(store.trades[0].t)} · row ${fmtP(step, decimalsOf(step))}` : '';
    painter.mark();
  }

  function onTrades(): void {
    if (!fb) return;
    const n = store.tradeSeq - seq;
    if (n <= 0) return;
    seq = store.tradeSeq;
    for (const t of store.trades.slice(-Math.min(n, store.trades.length))) {
      const bar = fb.add(t);
      analysis.delete(bar);
    }
    painter.mark();
  }

  function an(bar: FootprintBar): FootprintAnalysis {
    const c = analysis.get(bar);
    if (c && c.n === bar.trades) return c.a;
    const a = analyzeBar(bar, step, p.ratio, p.minVol, p.stack);
    analysis.set(bar, { a, n: bar.trades });
    return a;
  }

  function draw(): void {
    const { ctx, w, h } = fitCanvas(canvas);
    ctx.fillStyle = '#0b0e14';
    ctx.fillRect(0, 0, w, h);
    const key = `${store.key}|${store.tf}|${store.ticksPerBar}|${p.stepTicks}|${store.tradesFrom}`;
    if (!fb || key !== builtKey) rebuild();
    const bars = fb!.bars;
    if (!bars.length) {
      empty.textContent = 'No trades recorded yet for this instrument. The footprint is built only from real aggressive trades.';
      return;
    }
    empty.textContent = '';
    const profW = w < 600 ? 0 : 90;
    const footH = 54;
    const axisW = 64;
    const W = w - axisW - profW;
    const H = h - footH;
    const nVis = Math.max(1, Math.floor(W / p.colW));
    const endIdx = Math.max(0, bars.length - 1 - Math.max(0, Math.round(scrollBars)));
    const startIdx = Math.max(0, endIdx - nVis + 1);
    const vis = bars.slice(startIdx, endIdx + 1);
    const last = bars[bars.length - 1];
    if (followPrice || !isFinite(centerPrice)) centerPrice = last.c;
    const rowsVis = Math.floor(H / p.rowH);
    const topRow = rowOf(centerPrice, step) + Math.floor(rowsVis / 2);
    const yOfRow = (r: number) => (topRow - r) * p.rowH;
    // session delta (UTC day) across all loaded bars
    const sess = new Map<FootprintBar, number>();
    let acc = 0;
    let day = -1;
    for (const b of bars) {
      const d = Math.floor(b.t / 86_400_000);
      if (d !== day) {
        day = d;
        acc = 0;
      }
      acc += b.buy - b.sell;
      sess.set(b, acc);
    }
    const now = store.now();
    const evs = store.eventList().filter((e) => (e.kind === 'absorption' || e.kind === 'iceberg') && e.t >= (vis[0]?.t ?? now));
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = 'middle';
    let maxCell = 0;
    for (const b of vis) for (const r of b.rows.values()) maxCell = Math.max(maxCell, r.buy + r.sell);
    const barMs = store.tf === 'tick' ? 0 : TF_MS[store.tf as keyof typeof TF_MS];
    const x0 = Math.max(0, W - vis.length * p.colW); // latest bar at the right edge
    vis.forEach((b, i) => {
      const x = x0 + i * p.colW;
      const a = an(b);
      const cw = p.colW - 6;
      // candle body strip
      const up = b.c >= b.o;
      ctx.fillStyle = up ? '#26a69a' : '#ef5350';
      const yo = yOfRow(rowOf(b.o, step));
      const yc = yOfRow(rowOf(b.c, step));
      ctx.fillRect(x + 1, Math.min(yo, yc), 3, Math.abs(yo - yc) + p.rowH);
      for (const [r, row] of b.rows) {
        const y = yOfRow(r);
        if (y < -p.rowH || y > H) continue;
        const d = row.buy - row.sell;
        const tot = row.buy + row.sell;
        const alpha = Math.min(0.75, 0.08 + (tot / (maxCell || 1)) * 0.7);
        ctx.fillStyle = d >= 0 ? `rgba(38,166,154,${alpha})` : `rgba(239,83,80,${alpha})`;
        ctx.fillRect(x + 6, y, cw, p.rowH - 1);
        if (r === a.poc) {
          ctx.strokeStyle = '#ffd54f';
          ctx.lineWidth = 1.5;
          ctx.strokeRect(x + 6.5, y + 0.5, cw - 1, p.rowH - 2);
          ctx.lineWidth = 1;
        }
        const imb = a.imbalances.get(r);
        let txt: string;
        if (p.mode === 'bidask') txt = `${fmtQ(row.sell)} × ${fmtQ(row.buy)}`;
        else if (p.mode === 'delta') txt = fmtQ(d);
        else txt = fmtQ(tot);
        ctx.fillStyle = imb === 1 ? '#69f0ae' : imb === -1 ? '#ff8a80' : '#e0e6f0';
        ctx.font = imb ? 'bold 11px ui-monospace, Menlo, monospace' : '11px ui-monospace, Menlo, monospace';
        if (p.rowH >= 11 && p.colW >= 60) ctx.fillText(txt, x + 9, y + p.rowH / 2);
        if (imb) {
          ctx.fillStyle = imb === 1 ? '#69f0ae' : '#ff8a80';
          ctx.fillRect(imb === 1 ? x + 6 + cw - 3 : x + 6, y, 3, p.rowH - 1);
        }
      }
      for (const [lo, hi, dir] of a.stacked) {
        ctx.fillStyle = dir === 1 ? '#00e676' : '#ff1744';
        ctx.fillRect(x + p.colW - 3, yOfRow(hi), 3, (hi - lo + 1) * p.rowH);
      }
      ctx.fillStyle = '#ffab40';
      if (a.unfinishedHigh) ctx.fillText('UA', x + cw - 12, yOfRow(rowOf(b.h, step)) - 6);
      if (a.unfinishedLow) ctx.fillText('UA', x + cw - 12, yOfRow(rowOf(b.l, step)) + p.rowH + 6);
      // absorption / iceberg events inside this bar
      const bEnd = barMs ? b.t + barMs : (bars[startIdx + i + 1]?.t ?? Infinity);
      for (const e of evs) {
        if (e.t < b.t || e.t >= bEnd) continue;
        const y = yOfRow(rowOf(e.price, step)) + p.rowH / 2;
        ctx.fillStyle = e.kind === 'iceberg' ? '#b388ff' : '#ff9800';
        ctx.beginPath();
        ctx.arc(x + 10, y, 4, 0, Math.PI * 2);
        ctx.fill();
      }
      // footer
      const fy = H + 4;
      ctx.fillStyle = '#121722';
      ctx.fillRect(x, H, p.colW, footH);
      ctx.font = '10px ui-monospace, Menlo, monospace';
      ctx.fillStyle = a.delta >= 0 ? '#26a69a' : '#ef5350';
      ctx.fillText(`Δ ${fmtQ(a.delta)}`, x + 4, fy + 7);
      ctx.fillStyle = '#aab2c5';
      ctx.fillText(`V ${fmtQ(a.volume)}`, x + 4, fy + 20);
      const sd = sess.get(b) ?? 0;
      ctx.fillStyle = sd >= 0 ? '#26a69a' : '#ef5350';
      ctx.fillText(`ΣΔ ${fmtQ(sd)}`, x + 4, fy + 33);
      ctx.fillStyle = '#8a93a6';
      ctx.fillText(new Date(b.t).toISOString().slice(11, store.tf === '1s' || store.tf === 'tick' ? 19 : 16), x + 4, fy + 46);
    });
    // profile of visible bars (HVN / LVN)
    if (profW) {
      const m = new Map<number, ProfileRow>();
      let total = 0;
      for (const b of vis)
        for (const [r, row] of b.rows) {
          const pr = m.get(r) ?? { row: r, price: r * step, buy: 0, sell: 0, vol: 0 };
          pr.buy += row.buy;
          pr.sell += row.sell;
          pr.vol += row.buy + row.sell;
          total += row.buy + row.sell;
          m.set(r, pr);
        }
      const prof = finishProfile([...m.values()].sort((a, b) => a.row - b.row), step, total, 0.7, vis[0].t, vis[vis.length - 1].t);
      const maxV = Math.max(...prof.rows.map((r) => r.vol), 1);
      const px0 = W + axisW;
      for (const r of prof.rows) {
        const y = yOfRow(r.row);
        if (y < -p.rowH || y > H) continue;
        const inVA = r.price >= prof.val && r.price <= prof.vah;
        ctx.fillStyle = r.price === prof.poc ? '#ffd54f' : inVA ? '#4ea1ff99' : '#4ea1ff44';
        ctx.fillRect(px0, y + 1, (r.vol / maxV) * (profW - 4), p.rowH - 2);
      }
      ctx.font = '9px sans-serif';
      for (const hv of prof.hvn) {
        ctx.fillStyle = '#ffd54f';
        ctx.fillText('HVN', px0 + profW - 26, yOfRow(Math.round(hv / step)) + p.rowH / 2);
      }
      for (const lv of prof.lvn) {
        ctx.fillStyle = '#90a4ae';
        ctx.fillText('LVN', px0 + profW - 26, yOfRow(Math.round(lv / step)) + p.rowH / 2);
      }
    }
    // price axis
    ctx.fillStyle = '#121722';
    ctx.fillRect(W, 0, axisW, H);
    ctx.fillStyle = '#aab2c5';
    ctx.font = '10px sans-serif';
    const every = Math.max(1, Math.ceil(14 / p.rowH));
    for (let i = 0; i < rowsVis; i += every) {
      const r = topRow - i;
      ctx.fillText(fmtP(r * step, dec()), W + 4, i * p.rowH + p.rowH / 2);
    }
    const lp = store.lastTrade?.price;
    if (lp) {
      const y = yOfRow(rowOf(lp, step));
      ctx.fillStyle = '#fff';
      ctx.fillRect(W, y, axisW, p.rowH);
      ctx.fillStyle = '#000';
      ctx.fillText(fmtP(lp, dec()), W + 4, y + p.rowH / 2);
    }
  }

  gestures(canvas, {
    drag: (dx, dy) => {
      scrollBars = Math.max(0, scrollBars + dx / p.colW);
      if (Math.abs(dy) > 1) {
        followPrice = false;
        centerPrice += (dy / p.rowH) * step;
      }
      liveBtn.classList.toggle('on', scrollBars < 0.5 && followPrice);
      painter.mark();
    },
    zoom: (f, _x, _y, axis) => {
      if (axis === 'y') p.rowH = Math.max(8, Math.min(40, Math.round(p.rowH / f)));
      else p.colW = Math.max(40, Math.min(260, Math.round(p.colW / f)));
      save();
      painter.mark();
    },
    hover: (x, y) => {
      if (!fb) return;
      const H = canvas.clientHeight - 54;
      const W = canvas.clientWidth - 64 - (canvas.clientWidth < 600 ? 0 : 90);
      const nVis = Math.max(1, Math.floor(W / p.colW));
      const endIdx = Math.max(0, fb.bars.length - 1 - Math.round(scrollBars));
      const startIdx = Math.max(0, endIdx - nVis + 1);
      const x0 = Math.max(0, W - (endIdx - startIdx + 1) * p.colW);
      const i = Math.floor((x - x0) / p.colW);
      const b = i >= 0 ? fb.bars[startIdx + i] : undefined;
      if (!b || y > H || x > W) {
        tip.style.display = 'none';
        return;
      }
      const rowsVis = Math.floor(H / p.rowH);
      const topRow = rowOf(centerPrice, step) + Math.floor(rowsVis / 2);
      const r = topRow - Math.floor(y / p.rowH);
      const row = b.rows.get(r);
      const a = an(b);
      tip.textContent = `${fmtDateTime(b.t)}\nprice ${fmtP(r * step, dec())}\nsell@bid ${fmtQ(row?.sell ?? 0)}  buy@ask ${fmtQ(row?.buy ?? 0)}\nrow Δ ${fmtQ((row?.buy ?? 0) - (row?.sell ?? 0))}\nbar Δ ${fmtQ(a.delta)}  vol ${fmtQ(a.volume)}  trades ${b.trades}${a.imbalances.has(r) ? '\nimbalance ' + (a.imbalances.get(r) === 1 ? 'buy' : 'sell') : ''}${r === a.poc ? '\nPOC' : ''}`;
      tip.style.display = 'block';
      tip.style.left = Math.min(x + 12, canvas.clientWidth - 200) + 'px';
      tip.style.top = Math.min(y + 12, canvas.clientHeight - 130) + 'px';
    },
    leave: () => (tip.style.display = 'none'),
  });
  liveBtn.onclick = () => {
    scrollBars = 0;
    followPrice = true;
    liveBtn.classList.add('on');
    painter.mark();
  };
  const onSetting = () => {
    p.stepTicks = +stepSel.value;
    p.ratio = Math.max(1.1, +ratioIn.value || 3);
    p.minVol = Math.max(0, +minVolIn.value || 0);
    p.stack = Math.max(2, Math.round(+stackIn.value || 3));
    p.mode = modeSel.value as Prefs['mode'];
    save();
    analysis.clear();
    builtKey = '';
    painter.mark();
  };
  for (const i of [stepSel, ratioIn, minVolIn, stackIn, modeSel]) i.addEventListener('change', onSetting);
  store.on('trades', onTrades);
  store.on('history', () => {
    builtKey = '';
    painter.mark();
  });
  store.on('tf', () => {
    builtKey = '';
    painter.mark();
  });
  store.on('reset', () => {
    fb = null;
    builtKey = '';
    scrollBars = 0;
    followPrice = true;
    painter.mark();
  });
  store.on('events', () => painter.mark());
  new ResizeObserver(() => painter.mark()).observe(fill);
  return {
    id: 'footprint',
    title: 'Footprint',
    root,
    show: () => painter.show(),
    hide: () => painter.hide(),
  };
}
