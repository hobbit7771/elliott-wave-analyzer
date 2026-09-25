// Order Book Heatmap: historical resting liquidity from recorded local-book snapshots (never from OHLC),
// with executions, adds, cancels, price, events, clusters, vacuums, replay and export.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, fitCanvas, gestures, Painter, fmtQ, fmtP, fmtTime, fmtDateTime, loadPref, savePref, KIND_COLOR } from '../util.js';
import type { HeatColumn, MarketEvent } from '../../core/types.js';

const SPANS: [string, number][] = [
  ['1m', 60_000],
  ['5m', 300_000],
  ['15m', 900_000],
  ['1h', 3600_000],
  ['4h', 4 * 3600_000],
  ['24h', 24 * 3600_000],
  ['7d', 7 * 24 * 3600_000],
];

interface Prefs {
  span: number;
  depthBuckets: number;
  minVol: number;
  minConf: number;
  contrast: number;
  log: boolean;
  bids: boolean;
  asks: boolean;
  trades: boolean;
  adds: boolean;
  cancels: boolean;
  price: 'line' | 'candles' | 'none';
  events: boolean;
  zones: boolean;
  large: boolean;
}
const DEF: Prefs = { span: 300_000, depthBuckets: 120, minVol: 0, minConf: 45, contrast: 1, log: false, bids: true, asks: true, trades: true, adds: false, cancels: true, price: 'line', events: true, zones: true, large: true };

// colour ramps: cold (weak) -> yellow/orange (elevated) -> red (large ask) / green (large bid)
function ramp(stops: [number, [number, number, number]][]): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const v = i / 255;
    let k = 0;
    while (k < stops.length - 2 && v > stops[k + 1][0]) k++;
    const [a, ca] = stops[k];
    const [b, cb] = stops[k + 1];
    const f = Math.min(1, Math.max(0, (v - a) / (b - a)));
    for (let j = 0; j < 3; j++) lut[i * 3 + j] = ca[j] + (cb[j] - ca[j]) * f;
  }
  return lut;
}
const BG: [number, number, number] = [11, 14, 20];
const COLD: [number, [number, number, number]][] = [
  [0, BG],
  [0.12, [18, 32, 96]],
  [0.3, [0, 120, 190]],
  [0.5, [0, 200, 210]],
  [0.65, [255, 215, 0]],
  [0.8, [255, 140, 0]],
];
const LUT_ASK = ramp([...COLD, [1, [255, 40, 40]]]);
const LUT_BID = ramp([...COLD, [1, [40, 230, 90]]]);

export function createHeatmapTab(): Tab {
  const root = el('section', { id: 'tab-heatmap', role: 'tabpanel' });
  const p = loadPref<Prefs>('heatPrefs', DEF);
  const save = () => savePref('heatPrefs', p);

  const spanSel = el('select', { 'aria-label': 'History period' });
  for (const [l, ms] of SPANS) spanSel.append(el('option', { value: String(ms), text: l }));
  spanSel.value = String(p.span);
  const depthSel = el('select', { 'aria-label': 'Book depth shown', title: 'Price range shown (buckets around price)' });
  for (const n of [30, 60, 120, 200, 300, 600]) depthSel.append(el('option', { value: String(n), text: `±${n / 2} bkt` }));
  depthSel.value = String(p.depthBuckets);
  const minVol = el('input', { type: 'number', min: '0', step: 'any', value: String(p.minVol), title: 'Hide liquidity below this size' });
  const minConf = el('input', { type: 'number', min: '0', max: '100', step: '5', value: String(p.minConf), title: 'Minimum event confidence' });
  const contrast = el('input', { type: 'range', min: '0.3', max: '3', step: '0.1', value: String(p.contrast), title: 'Intensity' });
  const pauseBtn = el('button', { text: 'Pause' });
  const replayBtn = el('button', { text: 'Replay' });
  const replaySpeed = el('select', { 'aria-label': 'Replay speed' });
  for (const s of [1, 2, 5, 10, 30, 60]) replaySpeed.append(el('option', { value: String(s), text: `${s}×` }));
  replaySpeed.value = '10';
  const replayFrom = el('select', { 'aria-label': 'Replay start' });
  for (const [l, ms] of SPANS.slice(1, 6)) replayFrom.append(el('option', { value: String(ms), text: `last ${l}` }));
  const clearBtn = el('button', { text: 'Clear view' });
  const csvBtn = el('button', { text: 'CSV' });
  const pngBtn = el('button', { text: 'PNG' });
  const liveBtn = el('button', { text: 'Live', class: 'on' });
  const optsBtn = el('button', { text: 'Layers' });
  const info = el('span', { class: 'muted' });
  root.append(
    el('div', { class: 'toolbar' }, el('label', {}, 'Period', spanSel), el('label', {}, 'Depth', depthSel), el('label', {}, 'Min vol', minVol), el('label', {}, 'Min conf', minConf), el('label', {}, 'Intensity', contrast), pauseBtn, liveBtn, el('span', { class: 'sep' }), replayFrom, replaySpeed, replayBtn, el('span', { class: 'sep' }), clearBtn, csvBtn, pngBtn, optsBtn, info),
  );
  const fill = el('div', { class: 'fill' });
  const canvas = el('canvas');
  const tip = el('div', { class: 'tooltip' });
  const layerPanel = el('div', { class: 'layers' });
  const empty = el('div', { class: 'empty' });
  fill.append(canvas, tip, layerPanel, empty);
  root.append(fill);

  const toggles: [keyof Prefs, string][] = [
    ['bids', 'Bid liquidity'],
    ['asks', 'Ask liquidity'],
    ['trades', 'Executions (bubbles)'],
    ['adds', 'Added liquidity'],
    ['cancels', 'Removed liquidity (cancels)'],
    ['events', 'Icebergs / sweeps / events'],
    ['zones', 'Clusters, absorption, vacuum'],
    ['large', 'Large orders (held / broken)'],
    ['log', 'Log intensity scale'],
  ];
  for (const [k, l] of toggles) {
    const cb = el('input', { type: 'checkbox' });
    cb.checked = p[k] as boolean;
    cb.onchange = () => {
      (p as unknown as Record<string, unknown>)[k] = cb.checked;
      save();
      painter.mark();
    };
    layerPanel.append(el('label', {}, cb, l));
  }
  const priceSel = el('select');
  for (const v of ['line', 'candles', 'none']) priceSel.append(el('option', { value: v, text: 'Price: ' + v }));
  priceSel.value = p.price;
  priceSel.onchange = () => {
    p.price = priceSel.value as Prefs['price'];
    save();
    painter.mark();
  };
  layerPanel.append(priceSel);
  optsBtn.onclick = () => layerPanel.classList.toggle('on');

  // view state
  let live = true;
  let paused = false;
  let viewEnd = 0; // when not live
  let centerPrice = NaN; // NaN => auto-center on price
  let pxPerBucket = 0; // derived from depth
  let replay: { cols: HeatColumn[]; events: MarketEvent[]; from: number; to: number; cursor: number; speed: number; lastWall: number } | null = null;
  let frozen: { end: number } | null = null;
  let fetchKey = '';
  const off = document.createElement('canvas');

  const dec = () => store.meta?.pricePrecision ?? 2;
  const cols = (): HeatColumn[] => (replay ? replay.cols.filter((c) => c.t <= replay!.cursor) : store.heat);
  const endT = (): number => (replay ? replay.cursor : frozen ? frozen.end : live ? store.now() : viewEnd);

  function bucketStep(): number {
    const c = store.heat[store.heat.length - 1] ?? replay?.cols[replay.cols.length - 1];
    return c ? c.step : (store.status?.heatStep ?? store.meta?.tickSize ?? 1);
  }

  function lastPrice(): number {
    const cs = cols();
    const c = cs[cs.length - 1];
    if (replay && c) return c.last;
    return store.lastTrade?.price ?? (c ? c.last : NaN);
  }

  const painter = new Painter(draw, 100);

  function findCol(cs: HeatColumn[], t: number): HeatColumn | null {
    let lo = 0;
    let hi = cs.length - 1;
    if (hi < 0 || t > cs[hi].t + 250 || t < cs[0].t - cs[0].dt) return null;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (cs[mid].t < t) lo = mid + 1;
      else hi = mid;
    }
    const c = cs[lo];
    return c.t - c.dt - 250 <= t ? c : null;
  }

  function draw(): void {
    const { ctx, w, h } = fitCanvas(canvas);
    const axisW = 64;
    const axisH = 18;
    const W = Math.max(10, w - axisW);
    const H = Math.max(10, h - axisH);
    ctx.fillStyle = '#0b0e14';
    ctx.fillRect(0, 0, w, h);
    const cs = cols();
    const t1 = endT();
    const t0 = t1 - p.span;
    const step = bucketStep();
    const half = (p.depthBuckets / 2) * step;
    const lp0 = lastPrice();
    // follow price while live, but only re-center when it leaves the middle 60% (no jitter)
    if (isFinite(lp0) && (!isFinite(centerPrice) || (live && Math.abs(lp0 - centerPrice) > half * 0.6))) centerPrice = lp0;
    const pHi = centerPrice + half;
    const pLo = centerPrice - half;
    pxPerBucket = H / p.depthBuckets;
    empty.textContent = cs.length ? '' : store.status?.synced ? 'Recording order-book snapshots… first column appears within a second.' : 'Waiting for a synced order book (heatmap uses real local-book snapshots only).';
    if (!cs.length || !isFinite(centerPrice)) return;
    const yOf = (price: number) => ((pHi - price) / (pHi - pLo)) * H;
    const xOf = (t: number) => ((t - t0) / (t1 - t0)) * W;

    // intensity normaliser: P99.5 of visible positive sizes (sampled)
    const sample: number[] = [];
    const vis = cs.filter((c) => c.t >= t0 && c.t - c.dt <= t1);
    const stride = Math.max(1, Math.floor(vis.length / 60));
    for (let i = 0; i < vis.length; i += stride) {
      const c = vis[i];
      const b0 = Math.floor((pLo - c.p0) / c.step);
      const b1 = Math.ceil((pHi - c.p0) / c.step);
      for (let b = Math.max(0, b0); b <= Math.min(c.n - 1, b1); b++) {
        if (c.bids[b] > 0) sample.push(c.bids[b]);
        if (c.asks[b] > 0) sample.push(c.asks[b]);
      }
    }
    sample.sort((a, b) => a - b);
    const norm = (sample[Math.floor(sample.length * 0.995)] || 1) / p.contrast;
    const lnorm = Math.log1p(norm);

    // heat layer at CSS resolution
    off.width = W;
    off.height = H;
    const octx = off.getContext('2d')!;
    const img = octx.createImageData(W, H);
    const px = img.data;
    const rowPrice = new Float64Array(H);
    for (let y = 0; y < H; y++) rowPrice[y] = pHi - ((y + 0.5) / H) * (pHi - pLo);
    let lastCol: HeatColumn | null = null;
    for (let x = 0; x < W; x++) {
      const t = t0 + ((x + 0.5) / W) * (t1 - t0);
      const c = findCol(cs, t);
      if (!c) continue;
      lastCol = c;
      for (let y = 0; y < H; y++) {
        const b = Math.floor((rowPrice[y] - c.p0) / c.step + 1e-9);
        const o = (y * W + x) * 4;
        if (b < 0 || b >= c.n) continue;
        const bq = p.bids ? c.bids[b] : 0;
        const aq = p.asks ? c.asks[b] : 0;
        if (bq < 0 || aq < 0) {
          // unknown (outside the synced book range)
          px[o] = 22;
          px[o + 1] = 22;
          px[o + 2] = 28;
          px[o + 3] = 255;
          continue;
        }
        const q = Math.max(bq, aq);
        if (q <= 0 || q < p.minVol) continue;
        const v = p.log ? Math.log1p(q) / lnorm : q / norm;
        const i = Math.min(255, Math.max(0, Math.round(v * 255))) * 3;
        const lut = bq >= aq ? LUT_BID : LUT_ASK;
        px[o] = lut[i];
        px[o + 1] = lut[i + 1];
        px[o + 2] = lut[i + 2];
        px[o + 3] = 255;
      }
    }
    octx.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, W, H);

    // flows per column
    const colW = (c: HeatColumn) => Math.max(1, (c.dt / (t1 - t0)) * W);
    if (p.cancels || p.adds || p.trades) {
      let maxExec = 0;
      for (const c of vis) for (const [, b, s] of c.exec) maxExec = Math.max(maxExec, b + s);
      for (const c of vis) {
        const x = xOf(c.t);
        const cw = colW(c);
        if (p.cancels)
          for (const [i, q] of c.rem) {
            if (q < p.minVol || q <= 0) continue;
            const y = yOf(c.p0 + (i + 0.5) * c.step);
            if (y < 0 || y > H) continue;
            ctx.fillStyle = `rgba(230,230,230,${Math.min(0.9, 0.25 + (q / norm) * 0.7)})`;
            ctx.fillRect(x - cw, y - 1, Math.max(1, cw), 2);
          }
        if (p.adds)
          for (const [i, q] of c.add) {
            if (q < p.minVol || q <= 0) continue;
            const y = yOf(c.p0 + (i + 0.5) * c.step);
            if (y < 0 || y > H) continue;
            ctx.fillStyle = `rgba(120,200,255,${Math.min(0.8, 0.2 + (q / norm) * 0.6)})`;
            ctx.fillRect(x - cw, y - 1, Math.max(1, cw), 2);
          }
        if (p.trades && maxExec > 0)
          for (const [i, bv, sv] of c.exec) {
            const tot = bv + sv;
            const y = yOf(c.p0 + (i + 0.5) * c.step);
            if (y < -10 || y > H + 10) continue;
            const r = 2 + 12 * Math.sqrt(tot / maxExec);
            ctx.beginPath();
            ctx.arc(x - cw / 2, y, r, 0, Math.PI * 2);
            ctx.fillStyle = bv >= sv ? 'rgba(38,166,154,0.55)' : 'rgba(239,83,80,0.55)';
            ctx.fill();
            ctx.strokeStyle = bv >= sv ? '#26a69a' : '#ef5350';
            ctx.stroke();
          }
      }
    }

    // price
    if (p.price !== 'none' && vis.length) {
      if (p.price === 'line') {
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        vis.forEach((c, i) => (i ? ctx.lineTo(xOf(c.t), yOf(c.last)) : ctx.moveTo(xOf(c.t), yOf(c.last))));
        ctx.stroke();
        ctx.lineWidth = 1;
        for (const [key, color] of [['bb', 'rgba(38,166,154,.8)'], ['ba', 'rgba(239,83,80,.8)']] as const) {
          ctx.strokeStyle = color;
          ctx.beginPath();
          vis.forEach((c, i) => (i ? ctx.lineTo(xOf(c.t), yOf(c[key])) : ctx.moveTo(xOf(c.t), yOf(c[key]))));
          ctx.stroke();
        }
      } else {
        // candles: group columns into ~8px bars using column high/low/last
        const barMs = Math.max(1000, ((t1 - t0) / W) * 8);
        let open = NaN;
        let hi = -Infinity;
        let lo = Infinity;
        let close = NaN;
        let bucket = NaN;
        const flush = () => {
          if (!isFinite(open)) return;
          const x = xOf((bucket + 1) * barMs) - 4;
          ctx.strokeStyle = ctx.fillStyle = close >= open ? '#26a69a' : '#ef5350';
          ctx.beginPath();
          ctx.moveTo(x, yOf(hi));
          ctx.lineTo(x, yOf(lo));
          ctx.stroke();
          ctx.fillRect(x - 3, Math.min(yOf(open), yOf(close)), 6, Math.max(1, Math.abs(yOf(open) - yOf(close))));
        };
        for (const c of vis) {
          const k = Math.floor(c.t / barMs);
          if (k !== bucket) {
            flush();
            bucket = k;
            open = isFinite(close) ? close : c.last;
            hi = -Infinity;
            lo = Infinity;
          }
          hi = Math.max(hi, c.hi || c.last, c.last);
          lo = Math.min(lo, c.lo || c.last, c.last);
          close = c.last;
        }
        flush();
      }
    }

    // zones: clusters, absorption, vacuum
    const events = replay ? replay.events.filter((e) => e.t <= replay!.cursor) : store.eventList();
    ctx.font = '10px sans-serif';
    if (p.zones) {
      const zoneEvents = events.filter((e) => (e.kind === 'absorption' || e.kind === 'cluster' || e.kind === 'vacuum') && e.priceHi !== undefined && e.confidence >= p.minConf);
      for (const e of zoneEvents) {
        const x0 = Math.max(0, xOf(e.t));
        const x1 = Math.min(W, xOf(e.endT ?? (e.kind === 'absorption' ? e.t + 5 * 60_000 : e.kind === 'vacuum' ? e.t + 30_000 : t1)));
        if (x1 < 0 || x0 > W) continue;
        const y0 = yOf(e.priceHi!);
        const y1 = yOf(e.price);
        const color = e.kind === 'cluster' ? (e.status === 'broken' ? '#bdbdbd' : e.side === 'bid' ? '#26a69a' : '#ef5350') : KIND_COLOR[e.kind];
        ctx.strokeStyle = color;
        ctx.fillStyle = color + (e.kind === 'vacuum' ? '18' : '26');
        ctx.fillRect(x0, y0, x1 - x0, Math.max(2, y1 - y0));
        ctx.setLineDash(e.kind === 'vacuum' ? [3, 3] : []);
        ctx.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0, Math.max(2, y1 - y0));
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.fillText(`${e.title} ${e.confidence}`, x0 + 3, y0 - 2);
      }
      if (!replay) {
        for (const c of store.clusters.list) {
          if (c.confidence < p.minConf || c.status === 'faded') continue;
          const x0 = Math.max(0, xOf(c.firstSeen));
          const x1 = Math.min(W, xOf(c.status === 'broken' ? c.lastSeen : t1));
          const y0 = yOf(c.hi);
          const y1 = yOf(c.lo);
          ctx.strokeStyle = c.status === 'broken' ? '#bdbdbd' : c.side === 'bid' ? '#26a69a' : '#ef5350';
          ctx.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0, Math.max(2, y1 - y0));
          ctx.fillStyle = ctx.strokeStyle;
          ctx.fillText(`${c.label} ${c.confidence}`, x0 + 3, y1 + 10);
        }
      }
    }
    // large orders lifetime lines: held vs pulled / broken / filled
    if (p.large && !replay) {
      for (const lo of store.large.list) {
        if (lo.confidence < p.minConf) continue;
        const y = yOf(lo.price);
        if (y < 0 || y > H) continue;
        const x0 = Math.max(0, xOf(lo.firstSeen));
        const x1 = Math.min(W, xOf(lo.lastSeen));
        const held = lo.status === 'active' || lo.status === 'partially_filled';
        ctx.strokeStyle = held ? (lo.side === 'bid' ? '#69f0ae' : '#ff8a80') : '#bdbdbd';
        ctx.lineWidth = 2;
        ctx.setLineDash(held ? [] : [4, 3]);
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.lineWidth = 1;
        ctx.fillStyle = ctx.strokeStyle;
        const label = held ? 'Held' : lo.status === 'broken' ? 'Broken' : lo.status === 'pulled' ? 'Pulled' : 'Filled';
        ctx.fillText(`${label} ${fmtQ(lo.peak)} c${lo.confidence}`, Math.min(x1 + 3, W - 90), y - 3);
      }
    }
    // point events
    if (p.events) {
      for (const e of events) {
        if (e.confidence < p.minConf) continue;
        if (!['iceberg', 'sweep', 'stop_run', 'spoofing', 'liquidity_pulled', 'volume_burst', 'imbalance', 'delta_divergence'].includes(e.kind)) continue;
        const x = xOf(e.t);
        const y = yOf(e.price);
        if (x < 0 || x > W || y < 0 || y > H) continue;
        const color = KIND_COLOR[e.kind];
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        if (e.kind === 'iceberg') {
          const x1 = Math.min(W, xOf(e.endT ?? t1));
          ctx.globalAlpha = 0.35;
          ctx.fillRect(x, y - pxPerBucket / 2, Math.max(2, x1 - x), Math.max(2, pxPerBucket));
          ctx.globalAlpha = 1;
          ctx.beginPath();
          ctx.arc(x, y, 6, 0, Math.PI * 2);
          ctx.lineWidth = 2;
          ctx.stroke();
          ctx.lineWidth = 1;
          ctx.fillText(`Probable Iceberg ${e.confidence}`, x + 8, y - 6);
        } else if (e.kind === 'sweep' || e.kind === 'stop_run') {
          const up = e.side === 'buy';
          ctx.beginPath();
          ctx.moveTo(x, y);
          ctx.lineTo(x - 5, y + (up ? 9 : -9));
          ctx.lineTo(x + 5, y + (up ? 9 : -9));
          ctx.closePath();
          ctx.fill();
          ctx.fillText(`${e.kind === 'sweep' ? 'Sweep' : 'Stop run'} ${e.confidence}`, x + 7, y + (up ? 12 : -4));
        } else {
          ctx.fillRect(x - 3, y - 3, 6, 6);
          ctx.fillText(`${e.title.split(' (')[0]} ${e.confidence}`, x + 5, y - 4);
        }
      }
    }

    // axes
    ctx.fillStyle = '#121722';
    ctx.fillRect(W, 0, axisW, h);
    ctx.fillRect(0, H, W, axisH);
    ctx.fillStyle = '#aab2c5';
    const ticks = 8;
    for (let i = 0; i <= ticks; i++) {
      const price = pLo + ((pHi - pLo) * i) / ticks;
      const y = yOf(price);
      ctx.fillText(fmtP(price, dec()), W + 4, Math.min(H - 2, Math.max(10, y + 3)));
      ctx.fillStyle = '#1b2230';
      ctx.fillRect(0, y, W, 1);
      ctx.fillStyle = '#aab2c5';
    }
    const nLabels = Math.max(2, Math.min(6, Math.floor(W / 110)));
    for (let i = 0; i <= nLabels; i++) {
      const t = t0 + ((t1 - t0) * i) / nLabels;
      const x = xOf(t);
      ctx.fillText(p.span >= 86_400_000 ? new Date(t).toISOString().slice(5, 16).replace('T', ' ') : fmtTime(t), Math.min(W - 50, Math.max(0, x - 24)), H + 13);
    }
    const lp = lastPrice();
    if (isFinite(lp)) {
      const y = yOf(lp);
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(W, y - 8, axisW, 16);
      ctx.fillStyle = '#000';
      ctx.fillText(fmtP(lp, dec()), W + 4, y + 4);
    }
    const colsVisible = vis.length;
    info.textContent = `${colsVisible} columns · bucket ${fmtP(step, Math.max(dec(), 0))} · scale ${fmtQ(norm)}${replay ? ` · REPLAY ${fmtDateTime(replay.cursor)}` : ''}${lastCol ? '' : ' · no data in view'}`;
  }

  // ---------- data loading ----------
  async function ensureHistory(): Promise<void> {
    if (replay) return;
    const t1 = endT();
    const t0 = t1 - p.span;
    if (store.heatFrom && t0 >= store.heatFrom - 2000) return;
    const key = `${store.key}|${t0 - (t0 % 10_000)}|${p.span}`;
    if (fetchKey === key) return;
    fetchKey = key;
    try {
      const r = await api<{ cols: HeatColumn[] }>('/api/heatmap', { source: store.source, symbol: store.symbol, from: Math.round(t0), to: Math.round(t1), maxCols: Math.max(300, Math.min(3000, canvas.clientWidth * 2)) });
      store.setHeatHistory(r.cols, t0);
      painter.mark();
    } catch (e) {
      info.textContent = 'history: ' + (e as Error).message;
    }
  }

  // ---------- interaction ----------
  gestures(canvas, {
    drag: (dx, dy) => {
      const W = canvas.clientWidth - 64;
      const H = canvas.clientHeight - 18;
      if (Math.abs(dx) > 0) {
        if (live) viewEnd = store.now();
        live = false;
        liveBtn.classList.remove('on');
        viewEnd -= (dx / W) * p.span;
      }
      if (Math.abs(dy) > 0) {
        if (!isFinite(centerPrice)) centerPrice = lastPrice();
        centerPrice += (dy / H) * p.depthBuckets * bucketStep();
        if (live) {
          // vertical pan while live keeps time live but stops auto-centering
          live = false;
          viewEnd = store.now();
          liveBtn.classList.remove('on');
        }
      }
      painter.mark();
      void ensureHistory();
    },
    zoom: (f, _x, _y, axis) => {
      if (axis === 'y') {
        p.depthBuckets = Math.max(10, Math.min(600, Math.round(p.depthBuckets * f)));
        depthSel.value = String(p.depthBuckets);
      } else {
        p.span = Math.max(30_000, Math.min(7 * 86_400_000, p.span * f));
        spanSel.value = String(SPANS.reduce((a, b) => (Math.abs(b[1] - p.span) < Math.abs(a[1] - p.span) ? b : a))[1]);
      }
      save();
      painter.mark();
      void ensureHistory();
    },
    hover: (x, y) => showTip(x, y),
    leave: () => (tip.style.display = 'none'),
  });
  canvas.addEventListener('dblclick', () => goLive());

  function showTip(x: number, y: number): void {
    const W = canvas.clientWidth - 64;
    const H = canvas.clientHeight - 18;
    if (x > W || y > H) {
      tip.style.display = 'none';
      return;
    }
    const t1 = endT();
    const t = t1 - p.span + (x / W) * p.span;
    const step = bucketStep();
    const half = (p.depthBuckets / 2) * step;
    const price = centerPrice + half - (y / H) * 2 * half;
    const c = findCol(cols(), t);
    let s = `${fmtDateTime(t)}\nprice ${fmtP(price, dec())}`;
    if (c) {
      const b = Math.floor((price - c.p0) / c.step + 1e-9);
      if (b >= 0 && b < c.n) {
        const bq = c.bids[b];
        const aq = c.asks[b];
        s += `\nbucket ${fmtP(c.p0 + b * c.step, dec())}\nbid ${bq < 0 ? 'n/a' : fmtQ(bq)}  ask ${aq < 0 ? 'n/a' : fmtQ(aq)}`;
        const ex = c.exec.find((e) => e[0] === b);
        if (ex) s += `\nexec buy ${fmtQ(ex[1])} sell ${fmtQ(ex[2])}`;
        const ad = c.add.find((e) => e[0] === b);
        const rm = c.rem.find((e) => e[0] === b);
        if (ad) s += `\nadded ${fmtQ(ad[1])}`;
        if (rm) s += `\ncancelled ${fmtQ(rm[1])}`;
      }
    }
    tip.textContent = s;
    tip.style.display = 'block';
    tip.style.left = Math.min(x + 12, canvas.clientWidth - 180) + 'px';
    tip.style.top = Math.min(y + 12, canvas.clientHeight - 120) + 'px';
  }

  function goLive(): void {
    live = true;
    frozen = null;
    paused = false;
    pauseBtn.classList.remove('on');
    pauseBtn.textContent = 'Pause';
    centerPrice = NaN;
    liveBtn.classList.add('on');
    stopReplay();
    painter.mark();
  }

  spanSel.onchange = () => {
    p.span = +spanSel.value;
    save();
    painter.mark();
    void ensureHistory();
  };
  depthSel.onchange = () => {
    p.depthBuckets = +depthSel.value;
    save();
    painter.mark();
  };
  minVol.onchange = () => {
    p.minVol = Math.max(0, +minVol.value || 0);
    save();
    painter.mark();
  };
  minConf.onchange = () => {
    p.minConf = +minConf.value;
    save();
    painter.mark();
  };
  contrast.oninput = () => {
    p.contrast = +contrast.value;
    save();
    painter.mark();
  };
  liveBtn.onclick = goLive;
  pauseBtn.onclick = () => {
    paused = !paused;
    pauseBtn.classList.toggle('on', paused);
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    if (replay) replay.lastWall = performance.now();
    frozen = paused && !replay ? { end: endT() } : null;
    painter.mark();
  };
  clearBtn.onclick = () => {
    store.heat = [];
    store.heatFrom = store.now();
    fetchKey = 'cleared';
    painter.mark();
  };
  csvBtn.onclick = () => {
    const t1 = endT();
    const q = new URLSearchParams({ source: store.source, symbol: store.symbol, from: String(Math.round(t1 - p.span)), to: String(Math.round(t1)) });
    window.open('/api/export/heatmap.csv?' + q.toString(), '_blank');
  };
  pngBtn.onclick = () => {
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = `heatmap_${store.symbol}_${Math.round(endT())}.png`;
    a.click();
  };

  // ---------- replay ----------
  let replayTimer = 0;
  async function startReplay(): Promise<void> {
    const to = store.now();
    const from = to - +replayFrom.value;
    replayBtn.textContent = 'Loading…';
    try {
      const [h, ev] = await Promise.all([
        api<{ cols: HeatColumn[] }>('/api/heatmap', { source: store.source, symbol: store.symbol, from: Math.round(from), to: Math.round(to), maxCols: 4000 }),
        api<MarketEvent[]>('/api/events', { source: store.source, symbol: store.symbol, from: Math.round(from), to: Math.round(to) }),
      ]);
      if (!h.cols.length) {
        info.textContent = 'No recorded heatmap data in that window.';
        replayBtn.textContent = 'Replay';
        return;
      }
      replay = { cols: h.cols, events: ev, from: h.cols[0].t, to: h.cols[h.cols.length - 1].t, cursor: h.cols[0].t, speed: +replaySpeed.value, lastWall: performance.now() };
      live = false;
      liveBtn.classList.remove('on');
      centerPrice = h.cols[0].last;
      replayBtn.textContent = 'Stop replay';
      replayBtn.classList.add('on');
      const tick = () => {
        if (!replay) return;
        const now = performance.now();
        if (!paused) replay.cursor = Math.min(replay.to, replay.cursor + (now - replay.lastWall) * replay.speed);
        replay.lastWall = now;
        const lp = lastPrice();
        const half = (p.depthBuckets / 2) * bucketStep();
        if (isFinite(lp) && Math.abs(lp - centerPrice) > half * 0.7) centerPrice = lp;
        painter.mark();
        if (replay.cursor >= replay.to) info.textContent += ' · replay finished';
        replayTimer = window.setTimeout(tick, 100);
      };
      tick();
    } catch (e) {
      info.textContent = 'replay: ' + (e as Error).message;
      replayBtn.textContent = 'Replay';
    }
  }
  function stopReplay(): void {
    if (!replay) return;
    replay = null;
    clearTimeout(replayTimer);
    replayBtn.textContent = 'Replay';
    replayBtn.classList.remove('on');
  }
  replayBtn.onclick = () => (replay ? goLive() : void startReplay());
  replaySpeed.onchange = () => replay && (replay.speed = +replaySpeed.value);

  // ---------- live ----------
  const mark = () => !paused && painter.mark();
  store.on('heat', mark);
  store.on('trades', () => live && mark());
  store.on('events', mark);
  store.on('reset', () => {
    stopReplay();
    centerPrice = NaN;
    fetchKey = '';
    painter.mark();
  });
  store.on('history', () => void ensureHistory());
  new ResizeObserver(() => painter.mark()).observe(fill);
  setInterval(() => live && !paused && painter.mark(), 1000);

  return {
    id: 'heatmap',
    title: 'Heatmap',
    root,
    show() {
      painter.show();
      void ensureHistory();
    },
    hide() {
      painter.hide();
    },
  };
}

