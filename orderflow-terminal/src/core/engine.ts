// MarketEngine: pure (IO-free) pipeline for one instrument.
//   normalized snapshot/diff/trade messages -> local book -> flow classification -> detectors, heatmap columns.
// Used identically by the live server session, the replay tool and the tests.
import type { BookSnapshot, BookStats, Candle, DepthDiff, HeatColumn, InstrumentMeta, MarketEvent, Trade } from './types.js';
import { BookSync, FlowClassifier, OfiTracker, OrderBook, type FlowRecord, type SyncMode } from './orderbook.js';
import { HeatmapRecorder } from './heatmap.js';
import { CandleBuilder, candleDelta } from './candles.js';
import { atr } from './indicators.js';
import { RingSamples } from './stats.js';
import { type DetectorConfig, DEFAULT_DETECTOR_CONFIG } from './detectors/config.js';
import type { DetectorContext, EmitInput } from './detectors/context.js';
import { LargeOrderDetector } from './detectors/largeOrders.js';
import { IcebergDetector } from './detectors/iceberg.js';
import { FlowDetectors } from './detectors/flow.js';
import { ClusterDetector } from './detectors/clusters.js';

export interface EngineOptions {
  syncMode: SyncMode;
  heatStep: number;
  heatHalfBuckets: number;
  heatIntervalMs: number;
  /** remove book levels further than this many ticks from the touch */
  pruneTicks: number;
  /** zone bucket for clusters / vacuum (defaults to heatStep) */
  zoneStep?: number;
  obiLevels?: number;
}

export interface EngineSink {
  event?(ev: MarketEvent): void;
  heat?(col: HeatColumn): void;
  flow?(recs: FlowRecord[]): void;
  gap?(reason: string): void;
  synced?(): void;
}

export class MarketEngine {
  readonly book: OrderBook;
  readonly sync: BookSync;
  readonly classifier: FlowClassifier;
  readonly ofi = new OfiTracker(10_000);
  heat: HeatmapRecorder;
  readonly m1: CandleBuilder;
  readonly large: LargeOrderDetector;
  readonly iceberg: IcebergDetector;
  readonly flow: FlowDetectors;
  readonly clusters: ClusterDetector;
  readonly ctx: DetectorContext;
  stats: BookStats | null = null;
  lastDataT = 0;
  trades = 0;
  diffs = 0;
  gaps = 0;
  sessionCvd = 0;
  private tradeQty = new RingSamples(5000);
  private lastHeat = 0;
  private lastScan = 0;
  private lastEval = 0;
  private recent: MarketEvent[] = [];
  private gate = false;
  private atrSeries: number[] = [];

  constructor(
    public meta: InstrumentMeta,
    public cfg: DetectorConfig = DEFAULT_DETECTOR_CONFIG,
    public opts: EngineOptions,
    private sink: EngineSink = {},
  ) {
    this.book = new OrderBook(meta.tickSize);
    this.sync = new BookSync(opts.syncMode);
    this.classifier = new FlowClassifier(meta.tickSize);
    this.heat = new HeatmapRecorder({ step: opts.heatStep, halfBuckets: opts.heatHalfBuckets, intervalMs: opts.heatIntervalMs }, meta.tickSize);
    this.m1 = new CandleBuilder('1m', 100, 600);
    const self = this;
    this.ctx = {
      meta,
      cfg,
      book: this.book,
      tick: meta.tickSize,
      zoneStep: opts.zoneStep ?? opts.heatStep,
      now: 0,
      stats: null,
      atr1m: NaN,
      atrAvg: NaN,
      cvd: 0,
      medianTradeQty: NaN,
      get gateOpen() {
        return self.gate && self.sync.state === 'synced';
      },
      emit: (ev) => this.emit(ev),
    };
    this.large = new LargeOrderDetector(this.ctx);
    this.iceberg = new IcebergDetector(this.ctx, (k) => this.large.spoofed.has(k));
    this.flow = new FlowDetectors(this.ctx);
    this.clusters = new ClusterDetector(this.ctx);
  }

  /** Change heatmap / zone bucket size (only before recording starts, e.g. after the first snapshot). */
  setHeatStep(step: number): void {
    this.opts.heatStep = step;
    this.opts.zoneStep = step;
    this.ctx.zoneStep = step;
    this.opts.pruneTicks = Math.max(this.opts.pruneTicks, Math.round(step / this.meta.tickSize) * this.opts.heatHalfBuckets * 2);
    this.heat = new HeatmapRecorder({ step, halfBuckets: this.opts.heatHalfBuckets, intervalMs: this.opts.heatIntervalMs }, this.meta.tickSize);
  }

  setConfig(cfg: DetectorConfig): void {
    this.cfg = cfg;
    this.ctx.cfg = cfg;
  }

  /** Data freshness gate (set by the feed owner using wall-clock staleness). */
  setGate(open: boolean): void {
    this.gate = open;
  }
  get gateOpen(): boolean {
    return this.ctx.gateOpen;
  }

  /** Seed 1m candles (from REST klines) so ATR / swings / divergence are warm immediately. */
  seedCandles(m1: Candle[]): void {
    const closed = m1.slice(0, -1);
    this.m1.load(m1);
    this.recomputeBarState(closed);
  }

  private recomputeBarState(closed: Candle[]): void {
    const a = atr(closed, 14);
    this.atrSeries = a.filter((v) => !isNaN(v)).slice(-240);
    this.ctx.atr1m = this.atrSeries.length ? this.atrSeries[this.atrSeries.length - 1] : NaN;
    this.ctx.atrAvg = this.atrSeries.length ? this.atrSeries.reduce((s, v) => s + v, 0) / this.atrSeries.length : NaN;
    let c = 0;
    const cv = closed.map((b) => (c += candleDelta(b)));
    this.flow.setBars(closed.slice(-120), cv.slice(-120));
  }

  // ---- ingestion ------------------------------------------------------------

  beginSync(): void {
    this.sync.reset();
  }

  onSnapshot(s: BookSnapshot): { ok: boolean; reason?: string } {
    this.book.applySnapshot(s);
    const r = this.sync.onSnapshot(s.lastUpdateId);
    this.classifier.reset();
    if (r.gap) {
      this.gaps++;
      this.sink.gap?.(r.reason ?? 'gap');
      return { ok: false, reason: r.reason };
    }
    for (const d of r.applied) this.applyDiff(d, false);
    this.sink.synced?.();
    return { ok: true };
  }

  /** Returns false when a sequence gap was detected (caller must fetch a new snapshot). */
  onDiff(d: DepthDiff): boolean {
    this.diffs++;
    this.lastDataT = Math.max(this.lastDataT, d.t);
    const r = this.sync.onDiff(d);
    if (r.gap) {
      this.gaps++;
      this.resetTransient();
      this.sink.gap?.(r.reason ?? 'gap');
      return false;
    }
    for (const x of r.applied) this.applyDiff(x, true);
    return true;
  }

  private applyDiff(d: DepthDiff, classify: boolean): void {
    const changes = this.book.applyDiff(d);
    const bb = this.book.bestBid;
    const ba = this.book.bestAsk;
    if (isFinite(bb) && isFinite(ba)) {
      const ofi = this.ofi.update(d.t, bb, this.book.qtyAt('bid', this.book.bestBidTick), ba, this.book.qtyAt('ask', this.book.bestAskTick));
      this.stats = this.book.stats(this.opts.obiLevels ?? this.cfg.imbalance.levels, ofi, d.t);
      this.ctx.stats = this.stats;
    }
    if (!classify) return;
    const recs = this.classifier.onDiff(d.t, changes);
    this.handleFlow(recs, d.t);
  }

  private handleFlow(recs: FlowRecord[], t: number): void {
    if (!recs.length) return;
    this.heat.onFlow(recs);
    this.ctx.now = t;
    this.large.onFlow(recs, t);
    this.iceberg.onFlow(recs, t);
    this.sink.flow?.(recs);
  }

  onTrade(tr: Trade): void {
    this.trades++;
    this.lastDataT = Math.max(this.lastDataT, tr.t);
    this.ctx.now = tr.t;
    this.sessionCvd += tr.side * tr.qty;
    this.ctx.cvd = this.sessionCvd;
    this.tradeQty.push(tr.qty);
    if (this.trades % 256 === 0) this.ctx.medianTradeQty = this.tradeQty.percentile(0.5);
    this.heat.onTrade(tr);
    const { opened } = this.m1.add(tr);
    if (opened) {
      const closed = this.m1.candles.slice(0, -1);
      if (closed.length) {
        const bar = closed[closed.length - 1];
        let c = 0;
        const cv = closed.map((b) => (c += candleDelta(b)));
        this.flow.onBarClose(bar, cv[cv.length - 1]);
        this.recomputeBarState(closed);
      }
    }
    if (this.sync.state === 'synced') {
      const recs = this.classifier.onTrade(tr, this.book);
      if (recs.length) this.handleFlow(recs, tr.t);
    }
    this.large.onTrade(tr);
    this.iceberg.onTrade(tr);
    this.clusters.onTrade(tr);
    this.flow.onTrade(tr);
  }

  /** Periodic work: heatmap columns, threshold scans, iceberg / absorption evaluation. */
  onTimer(now: number): HeatColumn | null {
    this.ctx.now = now;
    let col: HeatColumn | null = null;
    if (this.sync.state === 'synced') {
      if (!this.lastHeat) this.lastHeat = now;
      if (now - this.lastHeat >= this.opts.heatIntervalMs) {
        col = this.heat.snapshot(now, this.book);
        this.lastHeat = now;
        if (col) this.sink.heat?.(col);
        this.book.prune(this.opts.pruneTicks);
      }
      if (now - this.lastScan >= 500) {
        this.lastScan = now;
        this.large.scan(now);
        this.clusters.scan(now);
      }
      if (now - this.lastEval >= 250) {
        this.lastEval = now;
        this.iceberg.evaluate(now);
        this.flow.evaluate(now);
      }
    }
    return col;
  }

  resetTransient(): void {
    this.classifier.reset();
    this.iceberg.reset();
    this.large.reset();
    this.flow.reset();
    this.clusters.reset();
    this.ofi.reset();
  }

  private emit(ev: EmitInput): void {
    const full: MarketEvent = { ...ev, source: this.meta.source, symbol: this.meta.symbol };
    const i = this.recent.findIndex((e) => e.id === full.id);
    if (i >= 0) this.recent[i] = full;
    else {
      this.recent.push(full);
      if (this.recent.length > 1000) this.recent.splice(0, this.recent.length - 1000);
    }
    this.sink.event?.(full);
  }

  recentEvents(): MarketEvent[] {
    return this.recent.slice();
  }
}
