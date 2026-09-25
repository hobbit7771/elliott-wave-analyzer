// Trading Chart: TradingView lightweight-charts with real candles, indicators, order-flow panes,
// event markers pinned to real price/time, zones and layer toggles.
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type ISeriesPrimitive,
  type SeriesAttachedParameter,
  type Time,
  type UTCTimestamp,
  type SeriesMarker,
  type IPriceLine,
  type SeriesType,
} from 'lightweight-charts';
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, fmtQ, fmtP, fmtDateTime, loadPref, savePref, loadPrefRaw, KIND_COLOR, isBullish, ownerToken } from '../util.js';
import { LEVEL_COLORS, labelSlots, selectLevels, type LevelMark } from '../levels.js';
import { DrawingsPrimitive, loadDrawings, saveDrawings, type Drawing, type DrawingType } from '../drawings.js';
import { CandleBuilder, TF_MS, candleDelta, type Timeframe } from '../../core/candles.js';
import { atr, cvd, ema, macd, rsi, vwap } from '../../core/indicators.js';
import type { Candle, EventKind, MarketEvent } from '../../core/types.js';
import type { StaticLevel } from '../../core/staticLevels.js';

const LAYERS = {
  volume: 'Объём',
  ema9: 'EMA 9',
  ema18: 'EMA 18',
  ema50: 'EMA 50',
  ema200: 'EMA 200',
  vwap: 'VWAP',
  atr: 'ATR (14)',
  rsi: 'RSI (14)',
  macd: 'MACD (12,26,9)',
  cvd: 'CVD (с начала загруженной истории)',
  delta: 'Delta бара',
  large: 'Крупные уровни видимой ликвидности',
  iceberg: 'Предполагаемые айсберги и признаки пополнения',
  absorption: 'Зоны поглощения',
  clusters: 'Кластеры ликвидности',
  static: 'Статические уровни D1 (до 4, синие)',
  sweep: 'Sweep и stop-run',
  imbalance: 'Дисбаланс',
  other: 'Прочее (дивергенция, всплеск, снятие, спуфинг?, вакуум)',
  liq: 'Опубликованные ликвидации (не все ликвидации рынка)',
  levels: 'Рисунки и paper SL/TP',
  conf: 'Показывать score',
};
type LayerKey = keyof typeof LAYERS;
const DEFAULT_LAYERS: Record<LayerKey, boolean> = {
  volume: true,
  ema9: true,
  ema18: true,
  ema50: true,
  ema200: true,
  vwap: true,
  atr: false,
  rsi: false,
  macd: false,
  cvd: true,
  delta: true,
  large: true,
  iceberg: true,
  absorption: true,
  clusters: true,
  static: true,
  sweep: true,
  imbalance: false,
  other: false,
  liq: false,
  levels: true,
  conf: true,
};
const KIND_LAYER: Partial<Record<EventKind, LayerKey>> = {
  iceberg: 'iceberg',
  absorption: 'absorption',
  cluster: 'clusters',
  sweep: 'sweep',
  stop_run: 'sweep',
  imbalance: 'imbalance',
  delta_divergence: 'other',
  volume_burst: 'other',
  liquidity_pulled: 'other',
  spoofing: 'other',
  vacuum: 'other',
  large_order: 'large',
};

interface Zone {
  t0: number;
  t1: number | null;
  lo: number;
  hi: number;
  color: string;
  label: string;
}

/**
 * Draws the curated liquidity levels as thin horizontal lines with small labels (important ones in purple),
 * plus faint absorption zones without text, behind the candles.
 */
class ZonesPrimitive implements ISeriesPrimitive<Time> {
  zones: Zone[] = [];
  levels: LevelMark[] = [];
  private p: SeriesAttachedParameter<Time, SeriesType> | null = null;
  constructor(private toTime: (ms: number) => number | null) {}
  attached(p: SeriesAttachedParameter<Time, SeriesType>): void {
    this.p = p;
  }
  detached(): void {
    this.p = null;
  }
  update(z: Zone[], levels: LevelMark[] = this.levels): void {
    this.zones = z;
    this.levels = levels;
    this.p?.requestUpdate();
  }
  paneViews() {
    const self = this;
    return [
      {
        zOrder: () => 'bottom' as const,
        renderer: () => ({
          draw: (target: { useMediaCoordinateSpace: (f: (s: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => void) => void }) => {
            const p = self.p;
            if (!p) return;
            target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
              try {
              const ts = p.chart.timeScale();
              const xAt = (ms: number | null, dflt: number) => {
                if (ms === null) return dflt;
                const a = self.toTime(ms);
                if (a === null) return dflt;
                try {
                  return ts.timeToCoordinate(a as UTCTimestamp) ?? dflt;
                } catch {
                  return dflt; // time outside the loaded bars
                }
              };
              for (const z of self.zones) {
                const x0 = xAt(z.t0, 0);
                const x1 = xAt(z.t1, mediaSize.width);
                const y0 = p.series.priceToCoordinate(z.hi);
                const y1 = p.series.priceToCoordinate(z.lo);
                if (y0 === null || y1 === null) continue;
                ctx.fillStyle = z.color + '14';
                ctx.fillRect(x0, y0, Math.max(2, x1 - x0), Math.max(1, y1 - y0));
              }
              const items: { l: LevelMark; y: number; x0: number }[] = [];
              for (const l of self.levels) {
                const y = p.series.priceToCoordinate(l.price);
                if (y === null || y < 0 || y > mediaSize.height) continue;
                items.push({ l, y: Math.round(y) + 0.5, x0: Math.max(0, xAt(l.t0, 0)) });
              }
              // ordinary first, important on top
              items.sort((a, b) => Number(a.l.important) - Number(b.l.important));
              for (const it of items) {
                ctx.strokeStyle = it.l.important ? LEVEL_COLORS.important : LEVEL_COLORS[it.l.side] + 'b0';
                ctx.lineWidth = it.l.important ? 1.5 : 1;
                ctx.setLineDash(it.l.important ? [] : [4, 3]);
                ctx.beginPath();
                ctx.moveTo(it.x0, it.y);
                ctx.lineTo(mediaSize.width, it.y);
                ctx.stroke();
              }
              ctx.setLineDash([]);
              ctx.lineWidth = 1;
              ctx.font = '9px system-ui, sans-serif';
              const shown = labelSlots([...items].sort((a, b) => Number(b.l.important) - Number(a.l.important)), 10);
              for (const it of shown) {
                const txt = it.l.label;
                const w = ctx.measureText(txt).width;
                ctx.fillStyle = it.l.important ? LEVEL_COLORS.important : LEVEL_COLORS[it.l.side];
                ctx.fillText(txt, mediaSize.width - w - 4, it.y - 2);
              }
              } catch {
                /* series not ready (data being replaced): skip this frame */
              }
            });
          },
        }),
      },
    ];
  }
}

export function createChartTab(): Tab {
  const root = el('section', { id: 'tab-chart', role: 'tabpanel' });
  const layers = loadPref<Record<LayerKey, boolean>>('chartLayers', DEFAULT_LAYERS);
  let minConf = loadPrefRaw<number>('chartMinConf', 45);
  const layersBtn = el('button', { text: 'Слои' });
  const confInput = el('input', { type: 'number', min: '0', max: '100', step: '5', value: String(minConf), title: 'Минимальный score для меток и уровней' });
  const toolSel = el('select', { 'aria-label': 'Инструмент рисования' });
  for (const [v, l] of [['', 'Рисование: нет'], ['hline', 'Горизонтальный уровень'], ['trend', 'Трендовая линия'], ['fib', 'Фибоначчи: коррекция'], ['fibext', 'Фибоначчи: расширение']]) toolSel.append(el('option', { value: v, text: l }));
  const clearDraw = el('button', { text: 'Удалить рисунки' });
  const fitBtn = el('button', { text: 'Вписать' });
  const liveBtn = el('button', { text: 'LIVE', class: 'on', title: 'К текущему времени' });
  let maxLevels = loadPrefRaw<number>('chartMaxLevels', 6);
  const levelsSel = el('select', { 'aria-label': 'Сколько уровней показывать', title: 'Сколько сильнейших уровней показывать (фиолетовые — важные)' });
  for (const n of [0, 3, 6, 10, 15]) levelsSel.append(el('option', { value: String(n), text: n ? `Уровней: ${n}` : 'Уровни: скрыть' }));
  levelsSel.value = String(maxLevels);
  levelsSel.onchange = () => {
    maxLevels = +levelsSel.value;
    savePref('chartMaxLevels', maxLevels);
    refreshOverlays();
  };
  const barState = el('span', { class: 'badge' });
  const originNote = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, layersBtn, el('label', {}, 'Мин. score', confInput), levelsSel, toolSel, clearDraw, fitBtn, liveBtn, barState, originNote));
  const fill = el('div', { class: 'fill' });
  const host = el('div', { class: 'chart-host' });
  const legend = el('div', { class: 'legend' });
  const layerPanel = el('div', { class: 'layers' });
  const empty = el('div', { class: 'empty' });
  fill.append(host, legend, layerPanel, empty);
  root.append(fill);

  for (const k of Object.keys(LAYERS) as LayerKey[]) {
    const cb = el('input', { type: 'checkbox' });
    cb.checked = layers[k];
    cb.onchange = () => {
      layers[k] = cb.checked;
      savePref('chartLayers', layers);
      rebuild();
    };
    layerPanel.append(el('label', {}, cb, LAYERS[k]));
  }
  layersBtn.onclick = () => layerPanel.classList.toggle('on');
  confInput.onchange = () => {
    minConf = +confInput.value;
    savePref('chartMinConf', minConf);
    refreshOverlays();
  };

  let chart: IChartApi | null = null;
  let candleS: ISeriesApi<'Candlestick'> | null = null;
  let markers: ISeriesMarkersPluginApi<Time> | null = null;
  let zones: ZonesPrimitive | null = null;
  const lines = new Map<string, ISeriesApi<SeriesType>>();
  let priceLines: IPriceLine[] = [];
  let builder = new CandleBuilder('1m');
  let seq = 0;
  let loading = false;
  let loadedKey = '';
  let exhausted = false;
  let visible = false;
  let drawings: Drawing[] = [];
  let drawWhere = '';
  let drawPrim: DrawingsPrimitive | null = null;
  let paperOpen: { key: string; side: number; entry: number; sl?: number; tp?: number }[] = [];
  // tick bars use sequential synthetic times; map back to real time for labels
  let tickTimes: number[] = [];
  /** open time of the forming (LIVE) bar reported by the server; bars before it are CLOSED */
  let liveFrom = 0;

  const dec = () => store.meta?.pricePrecision ?? 2;
  const isTick = () => store.tf === 'tick';
  const toTime = (c: Candle, i: number): number => (isTick() ? i + 1 : Math.floor(c.t / 1000));
  /** map a real timestamp to the chart time of the bar containing it */
  const barTime = (ms: number): number | null => {
    const cs = builder.candles;
    if (!cs.length) return null;
    if (isTick()) {
      let lo = 0;
      let hi = cs.length - 1;
      if (ms < cs[0].t) return null;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (cs[mid].t <= ms) lo = mid;
        else hi = mid - 1;
      }
      return lo + 1;
    }
    const tf = store.tf as Exclude<Timeframe, 'tick'>;
    const bt = Math.floor(ms / TF_MS[tf]) * TF_MS[tf];
    if (bt < cs[0].t) return null;
    return Math.floor(Math.min(bt, cs[cs.length - 1].t) / 1000);
  };


  function rebuild(): void {
    chart?.remove();
    lines.clear();
    priceLines = [];
    chart = createChart(host, {
      autoSize: true,
      layout: { background: { color: '#0b0e14' }, textColor: '#aab2c5', panes: { separatorColor: '#242c3d', enableResize: true } },
      grid: { vertLines: { color: '#161c28' }, horzLines: { color: '#161c28' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#242c3d' },
      timeScale: { borderColor: '#242c3d', timeVisible: true, secondsVisible: store.tf === '1s' || isTick(), rightOffset: 5 },
      localization: {
        locale: safeLocale(),
        timeFormatter: (t: number) => fmtDateTime(isTick() ? (tickTimes[t - 1] ?? 0) : t * 1000),
        priceFormatter: (p: number) => p.toFixed(dec()),
      },
    });
    if (isTick()) {
      chart.applyOptions({
        timeScale: {
          tickMarkFormatter: (t: number) => {
            const ms = tickTimes[t - 1];
            return ms ? new Date(ms).toISOString().slice(11, 19) : '';
          },
        },
      });
    }
    candleS = chart.addSeries(CandlestickSeries, { upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350', priceFormat: { type: 'price', precision: dec(), minMove: store.meta?.tickSize ?? 0.01 } });
    markers = createSeriesMarkers(candleS, []);
    zones = new ZonesPrimitive(barTime);
    candleS.attachPrimitive(zones);
    drawPrim = new DrawingsPrimitive(barTime);
    candleS.attachPrimitive(drawPrim);
    drawPrim.update(drawings);
    if (layers.volume) {
      const v = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
      v.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      lines.set('volume', v);
    }
    const overlay: [LayerKey, string][] = [
      ['ema9', '#ffeb3b'],
      ['ema18', '#ff9800'],
      ['ema50', '#42a5f5'],
      ['ema200', '#ab47bc'],
      ['vwap', '#e0e0e0'],
    ];
    for (const [k, color] of overlay) if (layers[k]) lines.set(k, chart.addSeries(LineSeries, { color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, lineStyle: k === 'vwap' ? LineStyle.Dashed : LineStyle.Solid }));
    let pane = 1;
    if (layers.cvd) lines.set('cvd', chart.addSeries(LineSeries, { color: '#4ea1ff', lineWidth: 1, priceLineVisible: false, title: 'CVD' }, pane++));
    if (layers.delta) lines.set('delta', chart.addSeries(HistogramSeries, { priceLineVisible: false, title: 'Delta' }, pane++));
    if (layers.rsi) {
      const r = chart.addSeries(LineSeries, { color: '#ce93d8', lineWidth: 1, priceLineVisible: false, title: 'RSI' }, pane++);
      r.createPriceLine({ price: 70, color: '#555', lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false, title: '' });
      r.createPriceLine({ price: 30, color: '#555', lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false, title: '' });
      lines.set('rsi', r);
    }
    if (layers.macd) {
      const p = pane++;
      lines.set('macdH', chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, p));
      lines.set('macd', chart.addSeries(LineSeries, { color: '#4ea1ff', lineWidth: 1, priceLineVisible: false, title: 'MACD' }, p));
      lines.set('macdS', chart.addSeries(LineSeries, { color: '#ff9800', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, p));
    }
    if (layers.atr) lines.set('atr', chart.addSeries(LineSeries, { color: '#90a4ae', lineWidth: 1, priceLineVisible: false, title: 'ATR' }, pane++));
    // price pane keeps most of the height; indicator panes share the rest (~30% in total)
    const panes = chart.panes();
    if (panes.length > 1) {
      panes[0].setStretchFactor(7 * (panes.length - 1));
      for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(3);
    }
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !candleS) return renderLegend(builder.candles.length - 1);
      const t = param.time as number;
      const idx = isTick() ? t - 1 : builder.candles.findIndex((c) => Math.floor(c.t / 1000) === t);
      renderLegend(idx);
    });
    chart.subscribeClick((param) => {
      const tool = toolSel.value as DrawingType | '';
      if (!tool || !param.point || !candleS || param.time === undefined) return;
      const price = candleS.coordinateToPrice(param.point.y);
      if (price === null) return;
      const t = param.time as number;
      const ms = isTick() ? (tickTimes[t - 1] ?? Date.now()) : t * 1000;
      const pt = { t: ms, p: +(+price).toFixed(dec()) };
      if (tool === 'hline') {
        drawings.push({ id: String(Date.now()), type: 'hline', a: pt });
        void persistDrawings();
        return;
      }
      if (!drawPrim?.pending) {
        if (drawPrim) drawPrim.pending = pt;
        refreshOverlays();
        return;
      }
      drawings.push({ id: String(Date.now()), type: tool, a: drawPrim.pending, b: pt });
      drawPrim.pending = null;
      void persistDrawings();
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (r && r.from < 10) void loadOlder();
      const atEnd = r ? r.to >= builder.candles.length - 2 : true;
      liveBtn.classList.toggle('on', atEnd);
    });
    setAll();
  }

  function setAll(): void {
    if (!chart || !candleS) return;
    const cs = builder.candles;
    tickTimes = isTick() ? cs.map((c) => c.t) : [];
    candleS.setData(cs.map((c, i) => ({ time: toTime(c, i) as UTCTimestamp, open: c.o, high: c.h, low: c.l, close: c.c })));
    const series = computeIndicators(cs);
    for (const [k, s] of lines) {
      const data = series[k];
      if (data) s.setData(data as never);
    }
    empty.textContent = cs.length ? '' : emptyText;
    refreshOverlays();
    renderLegend(cs.length - 1);
  }

  let emptyText = 'Загрузка…';

  type Pt = { time: UTCTimestamp; value: number; color?: string };
  function computeIndicators(cs: Candle[]): Record<string, Pt[]> {
    const t = cs.map((c, i) => toTime(c, i) as UTCTimestamp);
    const line = (vals: number[]): Pt[] => vals.map((v, i) => ({ time: t[i], value: v })).filter((p) => isFinite(p.value));
    const close = cs.map((c) => c.c);
    const out: Record<string, Pt[]> = {};
    if (lines.has('volume')) out.volume = cs.map((c, i) => ({ time: t[i], value: c.v, color: c.c >= c.o ? '#26a69a66' : '#ef535066' }));
    if (lines.has('ema9')) out.ema9 = line(ema(close, 9));
    if (lines.has('ema18')) out.ema18 = line(ema(close, 18));
    if (lines.has('ema50')) out.ema50 = line(ema(close, 50));
    if (lines.has('ema200')) out.ema200 = line(ema(close, 200));
    if (lines.has('vwap')) out.vwap = line(vwap(cs));
    if (lines.has('cvd')) out.cvd = line(cvd(cs));
    if (lines.has('delta')) out.delta = cs.map((c, i) => {
      const d = candleDelta(c);
      return { time: t[i], value: d, color: d >= 0 ? '#26a69a' : '#ef5350' };
    });
    if (lines.has('rsi')) out.rsi = line(rsi(cs, 14));
    if (lines.has('atr')) out.atr = line(atr(cs, 14));
    if (lines.has('macd')) {
      const m = macd(cs);
      out.macd = line(m.macd);
      out.macdS = line(m.signal);
      out.macdH = m.hist.map((v, i) => ({ time: t[i], value: v, color: v >= 0 ? '#26a69a88' : '#ef535088' })).filter((p) => isFinite(p.value));
    }
    return out;
  }

  /** live: update the last bar and the last point of every indicator */
  function updateLast(opened: boolean): void {
    try {
      updateLastUnsafe(opened);
    } catch (e) {
      // lightweight-charts rejected an incremental update (series being rebuilt): redraw from the builder
      console.warn('incremental update failed, full redraw:', (e as Error).message);
      try {
        setAll();
      } catch {
        /* next tick */
      }
    }
  }

  function updateLastUnsafe(opened: boolean): void {
    if (!chart || !candleS) return;
    const cs = builder.candles;
    const i = cs.length - 1;
    const c = cs[i];
    if (isTick() && opened) tickTimes.push(c.t);
    candleS.update({ time: toTime(c, i) as UTCTimestamp, open: c.o, high: c.h, low: c.l, close: c.c });
    // the last 1200 bars are enough for every indicator to converge (EMA200 residual < e^-10)
    const series = computeIndicators(cs.length > 1200 ? cs.slice(-1200) : cs);
    for (const [k, s] of lines) {
      const d = series[k];
      if (d && d.length) s.update(d[d.length - 1] as never);
    }
    if (opened) refreshOverlays();
    renderLegend(i);
  }

  function renderLegend(i: number): void {
    const c = builder.candles[i];
    if (!c) {
      legend.textContent = '';
      return;
    }
    const d = candleDelta(c);
    // CLOSED / LIVE: a bar is closed once its interval has ended (tick bars: once the next bar exists)
    const last = i === builder.candles.length - 1;
    const closed = isTick() ? !last : c.t + TF_MS[store.tf as Exclude<Timeframe, 'tick'>] <= Math.max(store.now(), liveFrom);
    barState.textContent = closed ? 'CLOSED' : 'LIVE';
    barState.className = 'badge ' + (closed ? 'syncing' : 'connected');
    clearDraw.title = drawWhere === 'server' ? 'Рисунки сохраняются в Supabase' : 'Рисунки сохранены только в этом браузере (нет токена владельца)';
    legend.innerHTML = '';
    legend.append(
      el('span', { text: `${store.symbol} ${store.tf}` }),
      el('span', { text: `O ${fmtP(c.o, dec())} H ${fmtP(c.h, dec())} L ${fmtP(c.l, dec())} C ${fmtP(c.c, dec())}` }),
      el('span', { text: `V ${fmtQ(c.v)}` }),
      el('span', { class: d >= 0 ? 'pos' : 'neg', text: `Δ ${fmtQ(d)}` }),
      el('span', { class: 'muted', text: fmtDateTime(c.t) }),
    );
  }

  function eventVisible(e: MarketEvent): boolean {
    const lk = KIND_LAYER[e.kind];
    return !!lk && layers[lk] && e.confidence >= minConf;
  }

  function refreshOverlays(): void {
    try {
      refreshOverlaysUnsafe();
    } catch (e) {
      // lightweight-charts throws while a series is being rebuilt; retry on the next tick instead of failing the page
      console.warn('overlay refresh skipped:', (e as Error).message);
      setTimeout(() => {
        try {
          refreshOverlaysUnsafe();
        } catch {
          /* next store update will redraw */
        }
      }, 300);
    }
  }

  function refreshOverlaysUnsafe(): void {
    if (!candleS || !markers || !zones) return;
    const evs = store.eventList().filter(eventVisible);
    const ms: SeriesMarker<Time>[] = [];
    const zs: Zone[] = [];
    for (const e of evs.slice(-150)) {
      const bt = barTime(e.t);
      if (bt === null) continue;
      const color = KIND_COLOR[e.kind];
      if (e.kind === 'absorption' && e.priceHi !== undefined) {
        zs.push({ t0: e.t, t1: e.endT ?? e.t + 15 * 60_000, lo: e.price, hi: e.priceHi, color, label: '' });
      }
      // clusters, vacuums and large levels are drawn as curated lines below, not as markers
      if (e.kind === 'large_order' || e.kind === 'cluster' || e.kind === 'vacuum') continue;
      const bull = isBullish(e);
      // short text only for strong events; the rest are plain glyphs (details on hover / Signals tab)
      const text = e.confidence >= 80 ? (layers.conf ? `${short(e)} ${e.confidence}` : short(e)) : '';
      ms.push({ time: bt as UTCTimestamp, position: 'atPriceMiddle', price: e.price, shape: bull === null ? 'circle' : bull ? 'arrowUp' : 'arrowDown', color, text, size: 0.6, id: e.id });
    }
    if (layers.liq) {
      for (const l of store.liqs) {
        const bt = barTime(l.t);
        if (bt !== null) ms.push({ time: bt as UTCTimestamp, position: 'atPriceMiddle', price: l.price, shape: 'square', color: l.side === 'sell' ? '#ef5350' : '#26a69a', text: '', size: 0.5 });
      }
    }
    ms.sort((a, b) => (a.time as number) - (b.time as number));
    markers.setMarkers(ms);
    const lv =
      layers.large || layers.clusters
        ? selectLevels(
            {
              large: layers.large ? store.large.list : [],
              clusters: layers.clusters ? store.clusters.list : [],
              events: store.eventList().slice(-300),
              now: store.now(),
              minConf,
            },
            maxLevels,
            store.meta?.tickSize ?? 0.01,
          )
        : [];
    zones.update(zs, lv);
    for (const pl of priceLines) candleS.removePriceLine(pl);
    priceLines = [];
    // static daily levels: solid blue lines across the whole chart with a price tag
    if (layers.static) for (const l of staticLv) priceLines.push(candleS.createPriceLine({ price: l.price, color: '#2962ff', lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: '' }));
    // important levels also get a compact tag on the price axis
    for (const l of lv) if (l.important) priceLines.push(candleS.createPriceLine({ price: l.price, color: LEVEL_COLORS.important, lineVisible: false, axisLabelVisible: true, title: '' }));
    if (layers.levels) {
      for (const d of drawings) if (d.type === 'hline') priceLines.push(candleS.createPriceLine({ price: d.a.p, color: '#ffd54f', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Уровень' }));
      for (const p of paperOpen.filter((x) => x.key === store.key)) {
        priceLines.push(candleS.createPriceLine({ price: p.entry, color: '#4ea1ff', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: `Paper ${p.side === 1 ? 'L' : 'S'}` }));
        if (p.sl !== undefined) priceLines.push(candleS.createPriceLine({ price: p.sl, color: '#ef5350', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'SL' }));
        if (p.tp !== undefined) priceLines.push(candleS.createPriceLine({ price: p.tp, color: '#26a69a', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'TP' }));
      }
      drawPrim?.update(drawings);
    }
  }

  function short(e: MarketEvent): string {
    switch (e.kind) {
      case 'iceberg':
        return 'ICE';
      case 'sweep':
        return 'SWP';
      case 'stop_run':
        return 'STOP';
      case 'imbalance':
        return 'IMB';
      case 'delta_divergence':
        return 'DIV';
      case 'volume_burst':
        return 'VOL';
      case 'liquidity_pulled':
        return 'PULL';
      case 'spoofing':
        return 'SPF?';
      default:
        return e.kind.slice(0, 4).toUpperCase();
    }
  }

  let staticLv: StaticLevel[] = [];
  let staticKey = '';
  async function loadStatic(): Promise<void> {
    const key = store.key;
    try {
      const r = await api<{ levels: StaticLevel[] }>('/api/levels/static', { source: store.source, symbol: store.symbol });
      if (key !== store.key) return;
      staticLv = r.levels ?? [];
      staticKey = key;
      store.staticLevels = staticLv;
      refreshOverlays();
    } catch {
      /* exchange REST unavailable and nothing stored yet: no static levels */
    }
  }
  setInterval(() => void loadStatic(), 30 * 60_000);

  async function load(): Promise<void> {
    if (staticKey !== store.key) {
      staticLv = [];
      void loadStatic();
    }
    const key = `${store.key}|${store.tf}|${store.ticksPerBar}`;
    loadedKey = key;
    exhausted = false;
    builder = new CandleBuilder(store.tf, store.ticksPerBar, 20_000);
    emptyText = 'Загрузка…';
    rebuild();
    const reqT = store.now();
    try {
      const r = await api<{ candles: Candle[]; origin: string; coverage?: { from: number; to: number; count: number }; liveFrom?: number; warning?: string }>('/api/klines', { source: store.source, symbol: store.symbol, tf: store.tf, limit: 1000, ticks: store.ticksPerBar });
      if (loadedKey !== key) return;
      builder.load(r.candles);
      // start consuming live trades that are newer than the loaded history
      // live trades that happened after the history request are applied on top
      seq = store.tradeSeq;
      for (const t of store.trades) if (t.t > reqT) builder.add(t);
      originNote.textContent =
        r.origin === 'recorded-trades'
          ? `Построено из записанных aggTrade${r.coverage?.from ? ' с ' + fmtDateTime(r.coverage.from) : ''} (покрытие ограничено записью; одно сообщение может объединять несколько исполнений)`
          : r.origin === 'stored-1m'
            ? `⚠ ${r.warning ?? 'REST биржи недоступен'} — закрытые 1m-свечи из Supabase`
            : 'Свечи REST биржи + живые сделки';
      originNote.className = r.origin === 'stored-1m' ? 'warn' : 'muted';
      liveFrom = r.liveFrom ?? 0;
      emptyText = r.origin === 'recorded-trades' ? 'Для инструмента ещё нет записанных сделок — бары появятся по мере поступления живых сделок.' : 'Биржа не вернула данных.';
      setAll();
      chart?.timeScale().scrollToRealTime();
    } catch (e) {
      emptyText = 'Не удалось загрузить историю: ' + (e as Error).message;
      setAll();
    }
  }

  async function loadOlder(): Promise<void> {
    if (loading || exhausted || isTick() || !builder.candles.length) return;
    loading = true;
    const key = loadedKey;
    try {
      const first = builder.candles[0].t;
      const r = await api<{ candles: Candle[] }>('/api/klines', { source: store.source, symbol: store.symbol, tf: store.tf, limit: 1000, end: first - 1, ticks: store.ticksPerBar });
      if (key !== loadedKey) return;
      const older = r.candles.filter((c) => c.t < first);
      if (!older.length) exhausted = true;
      else {
        const range = chart?.timeScale().getVisibleLogicalRange();
        builder.candles = older.concat(builder.candles);
        builder.maxBars = Math.max(builder.maxBars, builder.candles.length);
        setAll();
        if (range) chart?.timeScale().setVisibleLogicalRange({ from: range.from + older.length, to: range.to + older.length });
      }
    } catch {
      exhausted = true;
    } finally {
      loading = false;
    }
  }

  function onTrades(): void {
    if (!candleS) return;
    const n = store.tradeSeq - seq;
    if (n <= 0) return;
    seq = store.tradeSeq;
    const fresh = store.trades.slice(-Math.min(n, store.trades.length));
    let opened = false;
    for (const t of fresh) opened = builder.add(t).opened || opened;
    if (visible) updateLast(opened);
    else dirty = true;
  }
  let dirty = false;

  async function persistDrawings(): Promise<void> {
    drawWhere = await saveDrawings(store.key, drawings);
    refreshOverlays();
  }
  async function reloadDrawings(): Promise<void> {
    const r = await loadDrawings(store.key);
    drawings = r.list;
    drawWhere = r.where;
    refreshOverlays();
  }
  async function reloadPaper(): Promise<void> {
    if (!ownerToken()) return;
    try {
      const v = await api<{ open: typeof paperOpen }>('/api/paper');
      paperOpen = v.open;
      refreshOverlays();
    } catch {
      /* not authorized / not available */
    }
  }
  toolSel.onchange = () => {
    if (drawPrim) drawPrim.pending = null;
  };
  clearDraw.onclick = () => {
    if (!drawings.length || !confirm('Удалить все рисунки этого инструмента?')) return;
    drawings = [];
    void persistDrawings();
  };
  fitBtn.onclick = () => chart?.timeScale().fitContent();
  liveBtn.onclick = () => chart?.timeScale().scrollToRealTime();

  store.on('reset', () => void load());
  store.on('tf', () => void load());
  store.on('trades', onTrades);
  let ovTimer = 0;
  const overlaysSoon = () => {
    if (ovTimer) return;
    ovTimer = window.setTimeout(() => {
      ovTimer = 0;
      if (visible) refreshOverlays();
    }, 500);
  };
  store.on('events', overlaysSoon);
  store.on('large', overlaysSoon);
  store.on('clusters', overlaysSoon);
  store.on('meta', () => {
    candleS?.applyOptions({ priceFormat: { type: 'price', precision: dec(), minMove: store.meta?.tickSize ?? 0.01 } });
  });
  window.addEventListener('paper-changed', () => void reloadPaper());
  store.on('reset', () => void reloadDrawings());
  setInterval(() => visible && void reloadPaper(), 15_000);

  return {
    id: 'chart',
    title: 'График',
    root,
    show() {
      visible = true;
      if (!chart) void load();
      else if (dirty) {
        dirty = false;
        setAll();
      }
    },
    hide() {
      visible = false;
    },
  };
}

/** navigator.language can be a POSIX tag (e.g. "en-US@posix") that Intl rejects. */
function safeLocale(): string {
  const l = navigator.language || 'en-US';
  try {
    new Intl.NumberFormat(l);
    return l;
  } catch {
    return 'en-US';
  }
}

