// Live session for one instrument: WebSocket ingestion, snapshot sync / resync, staleness gating,
// recording and publishing. Runs inside a worker thread (see worker.ts) or inline in tests.
import type { Candle, ConnState, FeedStatus, HeatColumn, InstrumentMeta, MarketEvent, Trade } from '../core/types.js';
import { MarketEngine } from '../core/engine.js';
import { autoHeatStep } from '../core/heatmap.js';
import type { DetectorConfig } from '../core/detectors/config.js';
import type { MarketAdapter, NormalizedMsg } from './adapters/adapter.js';
import { ReconnectingWs, backoffDelay, type WsClientOptions } from './ingestion/wsClient.js';
import type { Recorder } from './recorder.js';
import { symKey } from './recorder.js';
import { Ewma } from '../core/stats.js';

export type Publish = (ch: string, data: unknown) => void;

export interface SessionOptions {
  publish: Publish;
  recorder?: Recorder;
  log?: (msg: string) => void;
  wsFactory?: WsClientOptions['factory'];
  bookLevels?: number;
  heatHalfBuckets?: number;
  heatIntervalMs?: number;
  backfillMinutes?: number;
  staleMs?: number;
  /** disable REST warm-up (tests) */
  skipWarmup?: boolean;
}

export class Session {
  readonly key: string;
  engine!: MarketEngine;
  ws!: ReconnectingWs;
  state: ConnState = 'connecting';
  private status: FeedStatus;
  private latency = new Ewma(0.1);
  private tradeBatch: [number, number, number, number, number][] = [];
  private timers: NodeJS.Timeout[] = [];
  private resyncTimer: NodeJS.Timeout | null = null;
  private resyncAttempt = 0;
  private lastDiffWall = 0;
  private bookDirty = false;
  private staleSince = 0;
  private lastRecordedBar = 0;
  private bboMismatchSince = 0;
  private deriv: { mark?: number; index?: number; funding?: number; nextFunding?: number; oi?: number; oiT?: number } = {};
  private lastBbo: { bid: number; ask: number; t: number } | null = null;
  private stopped = false;
  private log: (m: string) => void;

  constructor(
    public meta: InstrumentMeta,
    public adapter: MarketAdapter,
    public cfg: DetectorConfig,
    private o: SessionOptions,
  ) {
    this.key = symKey(meta.source, meta.symbol);
    this.log = o.log ?? (() => {});
    this.status = {
      source: meta.source,
      symbol: meta.symbol,
      state: 'connecting',
      lastUpdate: 0,
      lastEventTime: 0,
      latencyMs: NaN,
      trades: 0,
      depthUpdates: 0,
      depthLevels: 0,
      gaps: 0,
      resyncs: 0,
      reconnects: 0,
      dropped: 0,
      synced: false,
    };
  }

  /** Heatmap bucket: chosen from tick size and the expected snapshot range (refined after first snapshot). */
  private makeEngine(heatStep: number): MarketEngine {
    return new MarketEngine(
      this.meta,
      this.cfg,
      {
        syncMode: this.adapter.syncMode,
        heatStep,
        heatHalfBuckets: this.o.heatHalfBuckets ?? 300,
        heatIntervalMs: this.o.heatIntervalMs ?? 1000,
        pruneTicks: Math.max(5000, Math.round(heatStep / this.meta.tickSize) * (this.o.heatHalfBuckets ?? 300) * 2),
      },
      {
        event: (ev) => this.onEvent(ev),
        heat: (c) => this.onHeat(c),
        gap: (r) => this.onGap(r),
      },
    );
  }

  async start(): Promise<void> {
    this.engine = this.makeEngine(this.meta.tickSize * 10);
    this.engine.beginSync();
    this.ws = new ReconnectingWs({
      url: () => this.adapter.streamUrl(this.meta.symbol),
      silenceMs: 20_000,
      pingMs: 60_000,
      maxLifetimeMs: 23 * 3600_000,
      factory: this.o.wsFactory,
      onOpen: () => {
        this.setState('syncing', 'WebSocket open, syncing snapshot');
        this.engine.beginSync();
        // flow state from before the reconnect is not continuous with the new stream
        this.engine.resetTransient();
        this.scheduleResync(400);
      },
      onMessage: (raw) => this.onRaw(raw),
      onClose: (code, reason, will) => {
        this.status.reconnects = this.ws.reconnects;
        this.engine.setGate(false);
        this.status.synced = false;
        this.setState(will ? 'reconnecting' : 'disconnected', `socket closed ${code} ${reason}`);
        this.feedEvent(will ? 'Order-book disconnect — reconnecting' : 'Disconnected', `WebSocket closed (code ${code}${reason ? ', ' + reason : ''}).`);
      },
      onError: (e) => this.log(`[${this.key}] ws error: ${e.message}`),
    });
    if (!this.o.skipWarmup) void this.warmup();
    this.ws.start();
    this.timers.push(setInterval(() => this.flushTrades(), 100));
    this.timers.push(setInterval(() => this.publishBook(), 200));
    this.timers.push(setInterval(() => this.tick(), 250));
    this.timers.push(setInterval(() => this.everySecond(), 1000));
    if (this.adapter.fetchOpenInterest) {
      const poll = () => this.adapter.fetchOpenInterest!(this.meta.symbol).then((r) => {
        this.deriv.oi = r.oi;
        this.deriv.oiT = r.t;
        this.publishDeriv();
      }, (e) => this.log(`[${this.key}] OI poll failed: ${e.message}`));
      poll();
      this.timers.push(setInterval(poll, 15_000));
    }
  }

  private async warmup(): Promise<void> {
    try {
      const k = await this.adapter.fetchKlines(this.meta.symbol, '1m', 500);
      this.engine.seedCandles(k);
      this.lastRecordedBar = k.length > 1 ? k[k.length - 2].t : 0;
      for (const c of k.slice(0, -1)) this.o.recorder?.candle1m(c);
    } catch (e) {
      this.log(`[${this.key}] kline warm-up failed: ${(e as Error).message}`);
    }
    // backfill recent aggTrades so tick / 1s / footprint history exists right away
    const minutes = this.o.backfillMinutes ?? 10;
    if (!minutes || !this.o.recorder) return;
    try {
      const end = Date.now();
      let from = end - minutes * 60_000;
      for (let i = 0; i < 20 && from < end && !this.stopped; i++) {
        const batch = await this.adapter.fetchAggTrades(this.meta.symbol, from, Math.min(end, from + 3600_000 - 1));
        if (!batch.length) break;
        for (const t of batch) this.o.recorder.trade(t);
        from = batch[batch.length - 1].t + 1;
        if (batch.length < 1000) break;
      }
      this.o.recorder.flush(Date.now());
      this.o.publish('backfill', { from: end - minutes * 60_000, to: end });
    } catch (e) {
      this.log(`[${this.key}] aggTrades backfill failed: ${(e as Error).message}`);
    }
  }

  private scheduleResync(delay: number): void {
    if (this.resyncTimer || this.stopped) return;
    this.resyncTimer = setTimeout(() => {
      this.resyncTimer = null;
      void this.resync();
    }, delay);
  }

  private async resync(): Promise<void> {
    if (this.stopped || this.ws.state !== 'open') return;
    this.setState('syncing', 'fetching REST depth snapshot');
    try {
      const snap = await this.adapter.fetchSnapshot(this.meta.symbol);
      if (this.stopped) return;
      // choose heat bucket from the real snapshot range on first sync
      if (!this.status.synced && this.status.resyncs === 0) {
        const lo = snap.bids.length ? snap.bids[snap.bids.length - 1][0] : NaN;
        const hi = snap.asks.length ? snap.asks[snap.asks.length - 1][0] : NaN;
        if (isFinite(lo) && isFinite(hi)) {
          const step = autoHeatStep(this.meta.tickSize, hi - lo, 2 * (this.o.heatHalfBuckets ?? 300));
          if (step !== this.engine.opts.heatStep) this.engine.setHeatStep(step);
        }
      }
      const r = this.engine.onSnapshot(snap);
      this.status.resyncs++;
      if (!r.ok) {
        this.resyncAttempt++;
        this.log(`[${this.key}] snapshot not bridged: ${r.reason}`);
        this.scheduleResync(backoffDelay(this.resyncAttempt, 500, 10_000));
        return;
      }
      this.resyncAttempt = 0;
      this.status.synced = true;
      this.lastDiffWall = Date.now();
      this.engine.setGate(true);
      this.setState('connected', 'book synced');
      if (this.status.resyncs > 1) this.feedEvent('Resynchronization', `Local book rebuilt from REST snapshot #${snap.lastUpdateId} after a gap/reconnect.`);
      this.bookDirty = true;
    } catch (e) {
      this.resyncAttempt++;
      this.log(`[${this.key}] snapshot failed: ${(e as Error).message}`);
      this.setState('syncing', `snapshot failed: ${(e as Error).message.slice(0, 120)}`);
      this.scheduleResync(backoffDelay(this.resyncAttempt, 1000, 30_000));
    }
  }

  private onGap(reason: string): void {
    this.status.gaps++;
    this.status.synced = false;
    this.engine?.setGate(false);
    this.setState('gap', reason);
    this.feedEvent('Data gap', `Order-book sequence gap (${reason}). New signals blocked until resync.`);
    this.scheduleResync(50);
  }

  private onRaw(raw: string): void {
    const now = Date.now();
    this.status.lastUpdate = now;
    let msgs: NormalizedMsg[];
    try {
      msgs = this.adapter.parse(raw);
    } catch {
      this.status.dropped++;
      return;
    }
    for (const m of msgs) {
      switch (m.kind) {
        case 'diff': {
          this.status.depthUpdates++;
          this.lastDiffWall = now;
          this.noteLatency(now, m.eventTime);
          if (!this.engine.onDiff(m.d)) break;
          if (this.engine.sync.state === 'synced') this.bookDirty = true;
          break;
        }
        case 'trade':
          this.status.trades++;
          this.noteLatency(now, m.eventTime);
          this.onTrade(m.tr);
          break;
        case 'bbo':
          this.lastBbo = { bid: m.bid, ask: m.ask, t: now };
          break;
        case 'mark':
          this.deriv.mark = m.mark;
          this.deriv.index = m.index;
          this.deriv.funding = m.funding;
          this.deriv.nextFunding = m.nextFunding;
          break;
        case 'liq':
          this.o.publish('liq', m);
          break;
        default:
          break;
      }
    }
  }

  private noteLatency(now: number, et: number): void {
    if (et > 0) {
      this.latency.update(now - et);
      this.status.lastEventTime = et;
    }
  }

  private onTrade(tr: Trade): void {
    this.engine.onTrade(tr);
    this.o.recorder?.trade(tr);
    this.tradeBatch.push([tr.t, tr.price, tr.qty, tr.side, tr.id ?? 0]);
    if (this.tradeBatch.length > 5000) this.flushTrades();
  }

  private onEvent(ev: MarketEvent): void {
    this.o.recorder?.event(ev);
    this.o.publish('event', ev);
  }

  private onHeat(c: HeatColumn): void {
    this.o.recorder?.heat(c);
    this.o.publish('heat', c);
  }

  private feedEvent(title: string, explain: string): void {
    const t = Date.now();
    const ev: MarketEvent = { id: `feed-${t}-${title}`, t, kind: 'feed', title, price: this.engine?.stats?.mid ?? NaN, confidence: 100, explain, source: this.meta.source, symbol: this.meta.symbol };
    this.o.recorder?.event(ev);
    this.o.publish('event', ev);
  }

  private setState(s: ConnState, message?: string): void {
    if (this.state !== s) this.log(`[${this.key}] ${this.state} -> ${s}${message ? ' (' + message + ')' : ''}`);
    this.state = s;
    this.status.state = s;
    this.status.message = message;
    this.publishStatus();
  }

  private flushTrades(): void {
    if (!this.tradeBatch.length) return;
    this.o.publish('trades', this.tradeBatch);
    this.tradeBatch = [];
  }

  private publishBook(): void {
    if (!this.bookDirty || this.engine.sync.state !== 'synced') return;
    this.bookDirty = false;
    const n = this.o.bookLevels ?? 400;
    const top = this.engine.book.top(n);
    this.o.publish('book', { t: this.engine.book.lastT, bids: top.bids, asks: top.asks, stats: this.engine.stats, bbo: this.lastBbo });
  }

  private tick(): void {
    const now = Date.now();
    if (this.stopped) return;
    // staleness: no depth diff for staleMs while the socket claims to be open
    const staleMs = this.o.staleMs ?? this.cfg.staleMs;
    const synced = this.engine.sync.state === 'synced' && this.status.synced;
    const stale = synced && now - this.lastDiffWall > staleMs;
    if (stale) {
      if (!this.staleSince) {
        this.staleSince = now;
        this.engine.setGate(false);
        this.setState('stale', `no depth update for ${((now - this.lastDiffWall) / 1000).toFixed(1)}s`);
        this.feedEvent('Stale data', `No order-book update for more than ${staleMs / 1000}s. New signals are blocked.`);
      } else if (now - this.staleSince > 30_000) {
        this.staleSince = now;
        this.log(`[${this.key}] stale for 30s, forcing reconnect`);
        this.ws.restart();
      }
    } else if (this.staleSince && synced) {
      this.staleSince = 0;
      this.engine.setGate(true);
      this.setState('connected', 'data fresh again');
    }
    // cross-check local book against the exchange best bid/offer stream
    if (synced && this.lastBbo && now - this.lastBbo.t < 2000) {
      const b = this.engine.book;
      const tol = 5 * this.meta.tickSize;
      const mismatch = b.bestBid > this.lastBbo.ask + tol || b.bestAsk < this.lastBbo.bid - tol;
      if (mismatch) {
        if (!this.bboMismatchSince) this.bboMismatchSince = now;
        else if (now - this.bboMismatchSince > 3000) {
          this.bboMismatchSince = 0;
          this.engine.beginSync();
          this.onGap('local book crossed the exchange BBO for > 3s');
        }
      } else this.bboMismatchSince = 0;
    }
    this.engine.onTimer(now);
  }

  private everySecond(): void {
    const now = Date.now();
    this.status.latencyMs = Math.round(this.latency.value);
    this.status.depthLevels = this.engine.book.size;
    this.status.dropped = this.engine.sync.dropped;
    this.status.reconnects = this.ws.reconnects;
    this.publishStatus();
    this.o.publish('large', { thr: this.engine.large.thr, list: this.engine.large.list(now) });
    this.o.publish('clusters', { list: this.engine.clusters.list(), vacuums: this.engine.clusters.vacuums });
    this.o.publish('ice', this.engine.iceberg.candidates(now));
    this.publishDeriv();
    // record closed 1m bars built from live trades
    const bars = this.engine.m1.candles;
    for (let i = Math.max(0, bars.length - 5); i < bars.length - 1; i++) {
      if (bars[i].t > this.lastRecordedBar) {
        this.o.recorder?.candle1m(bars[i] as Candle);
        this.lastRecordedBar = bars[i].t;
      }
    }
    try {
      this.o.recorder?.flush(now);
    } catch (e) {
      this.log(`[${this.key}] recorder flush failed: ${(e as Error).message}`);
    }
  }

  private publishStatus(): void {
    this.o.publish('status', { ...this.status, heatStep: this.engine?.opts.heatStep, cvd: this.engine?.sessionCvd, atr1m: this.engine?.ctx.atr1m });
  }

  private publishDeriv(): void {
    if (this.adapter.caps.markPrice || this.adapter.caps.openInterest) this.o.publish('deriv', this.deriv);
  }

  setConfig(cfg: DetectorConfig): void {
    this.cfg = cfg;
    this.engine.setConfig(cfg);
  }

  getStatus(): FeedStatus {
    return this.status;
  }

  stop(): void {
    this.stopped = true;
    for (const t of this.timers) clearInterval(t);
    if (this.resyncTimer) clearTimeout(this.resyncTimer);
    this.ws?.stop();
    try {
      this.o.recorder?.flush(Date.now());
    } catch {
      /* ignore on shutdown */
    }
  }
}
