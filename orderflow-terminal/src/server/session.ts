// Live session for one instrument: per-route WebSocket ingestion, snapshot sync / resync, per-stream
// freshness and signal gating, trade-id continuity, ID-aware BBO cross-check, local + Supabase recording,
// live verification against the exchange REST API, publishing. Runs inside a worker thread (worker.ts).
import type { BookSnapshot, Candle, ConnState, FeedStatus, HeatColumn, InstrumentMeta, MarketEvent, Trade } from '../core/types.js';
import { MarketEngine } from '../core/engine.js';
import { autoHeatStep } from '../core/heatmap.js';
import type { DetectorConfig } from '../core/detectors/config.js';
import { restHealth, type MarketAdapter, type NormalizedMsg, type StreamRoute } from './adapters/adapter.js';
import { ReconnectingWs, backoffDelay, type WsClientOptions } from './ingestion/wsClient.js';
import type { Recorder } from './recorder.js';
import { symKey } from './recorder.js';
import { Ewma } from '../core/stats.js';
import type { PersistWriter } from './persist/writer.js';

export type Publish = (ch: string, data: unknown) => void;

export interface SessionOptions {
  publish: Publish;
  recorder?: Recorder;
  persist?: PersistWriter;
  log?: (msg: string) => void;
  wsFactory?: WsClientOptions['factory'];
  bookLevels?: number;
  heatHalfBuckets?: number;
  heatIntervalMs?: number;
  backfillMinutes?: number;
  staleMs?: number;
  /** no message at all on the trades/mark route for this long => that stream is stale */
  flowSilenceMs?: number;
  /** disable REST warm-up and periodic verification (tests) */
  skipWarmup?: boolean;
}

export interface StreamState {
  route: string;
  connected: boolean;
  lastMsgAt: number;
  messages: number;
  reconnects: number;
  url: string;
}

export interface Verification {
  bbo: { checks: number; matches: number; mismatches: number; pending: number; lastMismatch: string };
  trades: { idGaps: number; missingIds: number; backfilled: number; restSamples: number; restMatches: number; restMismatch: string; lastId: number; lastCheckAt: number };
  candles: { compared: number; ohlcMatches: number; volumeWithin: number; lastDiff: string; lastCheckAt: number };
}

export class Session {
  readonly key: string;
  engine!: MarketEngine;
  sockets = new Map<string, ReconnectingWs>();
  state: ConnState = 'connecting';
  private status: FeedStatus & { streams: Record<string, StreamState>; gate: boolean; gateReason: string; verify: Verification; since: number };
  private latency = new Ewma(0.1);
  private tradeBatch: [number, number, number, number, number][] = [];
  private timers: NodeJS.Timeout[] = [];
  private resyncTimer: NodeJS.Timeout | null = null;
  private resyncAttempt = 0;
  private lastDiffWall = 0;
  private bookDirty = false;
  private staleSince = 0;
  private lastRecordedBar = 0;
  private bboMismatchRun = 0;
  private bookHistory: { u: number; bb: number; ba: number }[] = [];
  private pendingBbo: { u: number; bid: number; ask: number }[] = [];
  private deriv: { mark?: number; index?: number; funding?: number; nextFunding?: number; oi?: number; oiT?: number; markAt?: number } = {};
  private lastBbo: { bid: number; ask: number; t: number; u?: number } | null = null;
  private lastTradeId = 0;
  private liveBars = new Map<number, Candle>();
  private firstLiveBar = 0;
  /** minutes during which trade ids were missing: their live-built bars are incomplete */
  private gapMinutes = new Set<number>();
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
      streams: {},
      gate: false,
      gateReason: 'starting',
      since: Date.now(),
      verify: {
        bbo: { checks: 0, matches: 0, mismatches: 0, pending: 0, lastMismatch: '' },
        trades: { idGaps: 0, missingIds: 0, backfilled: 0, restSamples: 0, restMatches: 0, restMismatch: '', lastId: 0, lastCheckAt: 0 },
        candles: { compared: 0, ohlcMatches: 0, volumeWithin: 0, lastDiff: '', lastCheckAt: 0 },
      },
    };
  }

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

  private hasDepth(r: StreamRoute): boolean {
    return r.route === 'depth' || r.route === 'all';
  }

  async start(): Promise<void> {
    this.engine = this.makeEngine(this.meta.tickSize * 10);
    this.engine.beginSync();
    for (const r of this.adapter.streamRoutes(this.meta.symbol)) {
      const st: StreamState = { route: r.route, connected: false, lastMsgAt: 0, messages: 0, reconnects: 0, url: r.url.replace(/\?.*$/, '') };
      this.status.streams[r.route] = st;
      const ws: ReconnectingWs = new ReconnectingWs({
        url: r.url,
        sendOnOpen: r.subscribe,
        appPing: r.appPing,
        silenceMs: 20_000,
        pingMs: 60_000,
        maxLifetimeMs: 23 * 3600_000,
        factory: this.o.wsFactory,
        onOpen: () => {
          st.connected = true;
          if (this.hasDepth(r)) {
            this.setState('syncing', 'WebSocket open, syncing snapshot');
            this.engine.beginSync();
            // flow state from before the reconnect is not continuous with the new stream
            this.engine.resetTransient();
            // venues that push the snapshot on the stream (Bybit) need no REST snapshot
            if (!this.adapter.snapshotViaStream) this.scheduleResync(400);
          }
          this.updateGate();
        },
        onMessage: (raw) => {
          st.lastMsgAt = Date.now();
          st.messages++;
          this.onRaw(raw);
        },
        onClose: (code, reason, will) => {
          st.connected = false;
          st.reconnects = ws.reconnects;
          this.status.reconnects = [...this.sockets.values()].reduce((s, x) => s + x.reconnects, 0);
          if (this.hasDepth(r)) {
            this.status.synced = false;
            this.o.persist?.onGap(Date.now(), `depth socket closed ${code}`);
            this.setState(will ? 'reconnecting' : 'disconnected', `depth socket closed ${code} ${reason}`);
            this.feedEvent(will ? 'Разрыв потока стакана — переподключение' : 'Отключено', `WebSocket (${r.route}) закрыт (код ${code}${reason ? ', ' + reason : ''}).`);
          } else {
            this.feedEvent('Разрыв потока сделок — переподключение', `WebSocket (${r.route}: сделки, mark, ликвидации) закрыт (код ${code}). Сигналы, зависящие от сделок, заблокированы.`);
          }
          this.updateGate();
        },
        onError: (e) => this.log(`[${this.key}] ws(${r.route}) error: ${e.message}`),
      });
      this.sockets.set(r.route, ws);
    }
    if (!this.o.skipWarmup) void this.warmup();
    for (const ws of this.sockets.values()) ws.start();
    this.timers.push(setInterval(() => this.flushTrades(), 100));
    this.timers.push(setInterval(() => this.publishBook(), 200));
    this.timers.push(setInterval(() => this.tick(), 250));
    this.timers.push(setInterval(() => this.everySecond(), 1000));
    if (!this.o.skipWarmup) {
      this.timers.push(setInterval(() => void this.verifyAgainstRest(), 60_000));
      this.timers.push(setInterval(() => this.logVerification(), 60_000));
    }
    if (this.adapter.fetchOpenInterest && !this.o.skipWarmup) {
      const poll = () =>
        this.adapter.fetchOpenInterest!(this.meta.symbol).then(
          (r) => {
            this.deriv.oi = r.oi;
            this.deriv.oiT = r.t;
            this.publishDeriv();
          },
          (e) => this.log(`[${this.key}] OI poll failed: ${e.message}`),
        );
      poll();
      this.timers.push(setInterval(poll, 30_000));
    }
  }

  private async warmup(): Promise<void> {
    try {
      const k = await this.adapter.fetchKlines(this.meta.symbol, '1m', 500);
      this.engine.seedCandles(k);
      this.lastRecordedBar = k.length > 1 ? k[k.length - 2].t : 0;
      for (const c of k.slice(0, -1)) {
        this.o.recorder?.candle1m(c);
        this.o.persist?.onCandle(c, 'rest');
      }
    } catch (e) {
      this.log(`[${this.key}] kline warm-up failed: ${(e as Error).message}`);
    }
    // backfill recent aggTrades so tick / 1s / footprint history exists right away (local cache only)
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

  private depthSocket(): ReconnectingWs | undefined {
    return this.sockets.get('depth') ?? this.sockets.get('all');
  }

  private async resync(): Promise<void> {
    if (this.stopped || this.depthSocket()?.state !== 'open') return;
    this.setState('syncing', 'fetching REST depth snapshot');
    try {
      const snap = await this.adapter.fetchSnapshot(this.meta.symbol);
      if (this.stopped) return;
      if (!this.status.synced && this.status.resyncs === 0) {
        const lo = snap.bids.length ? snap.bids[snap.bids.length - 1][0] : NaN;
        const hi = snap.asks.length ? snap.asks[snap.asks.length - 1][0] : NaN;
        if (isFinite(lo) && isFinite(hi)) {
          const step = autoHeatStep(this.meta.tickSize, hi - lo, 2 * (this.o.heatHalfBuckets ?? 300));
          if (step !== this.engine.opts.heatStep) this.engine.setHeatStep(step);
        }
      }
      this.applySnapshot(snap, 'REST');
    } catch (e) {
      this.resyncAttempt++;
      this.log(`[${this.key}] snapshot failed: ${(e as Error).message}`);
      this.setState('syncing', `snapshot failed: ${(e as Error).message.slice(0, 120)}`);
      this.scheduleResync(backoffDelay(this.resyncAttempt, 1000, 30_000));
    }
  }

  /** One path for every snapshot, from REST (Binance) or from the depth stream (Bybit). */
  private applySnapshot(snap: BookSnapshot, origin: 'REST' | 'stream'): void {
    const r = this.engine.onSnapshot(snap);
    this.status.resyncs++;
    if (!r.ok) {
      this.resyncAttempt++;
      this.log(`[${this.key}] snapshot not bridged: ${r.reason}`);
      if (origin === 'REST') this.scheduleResync(backoffDelay(this.resyncAttempt, 500, 10_000));
      else this.depthSocket()?.restart();
      return;
    }
    this.resyncAttempt = 0;
    this.status.synced = true;
    this.lastDiffWall = Date.now();
    this.bookHistory = [];
    this.pendingBbo = [];
    this.o.persist?.onSynced(this.engine.book, this.engine.book.lastT || Date.now());
    this.setState('connected', 'book synced');
    this.updateGate();
    if (this.status.resyncs > 1) this.feedEvent('Ресинхронизация', `Локальный стакан восстановлен по ${origin === 'REST' ? 'REST-снимку' : 'снимку из потока'} #${snap.lastUpdateId} после разрыва/переподключения.`);
    this.bookDirty = true;
  }

  private onGap(reason: string): void {
    this.status.gaps++;
    this.status.synced = false;
    this.o.persist?.onGap(Date.now(), reason);
    this.setState('gap', reason);
    this.updateGate();
    this.feedEvent('Разрыв данных', `Нарушена последовательность стакана (${reason}). Новые сигналы заблокированы до ресинхронизации.`);
    // stream-snapshot venues resend a snapshot only on a new connection
    if (this.adapter.snapshotViaStream) this.depthSocket()?.restart();
    else this.scheduleResync(50);
  }

  /** Signals require: synced + fresh depth AND a live trades stream (detectors join both). */
  private updateGate(): void {
    const now = Date.now();
    const depthOk = this.status.synced && this.engine?.sync.state === 'synced' && now - this.lastDiffWall <= (this.o.staleMs ?? this.cfg.staleMs);
    const flow = this.status.streams.flow ?? this.status.streams.all;
    const flowSilence = this.o.flowSilenceMs ?? 15_000;
    const flowOk = !!flow && flow.connected && (flow.lastMsgAt === 0 ? true : now - flow.lastMsgAt <= flowSilence);
    const open = !!depthOk && flowOk;
    this.status.gate = open;
    this.status.gateReason = open ? '' : !this.status.synced ? 'order book not synced' : !depthOk ? 'depth data stale' : 'trades stream down or silent';
    this.engine?.setGate(open);
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
          const wasSynced = this.engine.sync.state === 'synced';
          if (!this.engine.onDiff(m.d)) break;
          if (wasSynced && this.engine.sync.state === 'synced') {
            this.bookDirty = true;
            this.o.persist?.onDiff(m.d, this.engine.book);
            this.bookHistory.push({ u: m.d.lastId, bb: this.engine.book.bestBid, ba: this.engine.book.bestAsk });
            if (this.bookHistory.length > 600) this.bookHistory.splice(0, 100);
            this.checkBbo();
          }
          break;
        }
        case 'snapshot':
          this.status.depthUpdates++;
          this.lastDiffWall = now;
          this.engine.beginSync();
          this.applySnapshot(m.snap, 'stream');
          break;
        case 'trade':
          this.status.trades++;
          this.noteLatency(now, m.eventTime);
          this.onTrade(m.tr);
          break;
        case 'bbo':
          this.lastBbo = { bid: m.bid, ask: m.ask, t: now, u: m.u };
          if (m.u !== undefined && this.status.synced) {
            this.pendingBbo.push({ u: m.u, bid: m.bid, ask: m.ask });
            if (this.pendingBbo.length > 400) this.pendingBbo.splice(0, 200);
            this.checkBbo();
          }
          break;
        case 'mark':
          this.deriv.mark = m.mark;
          this.deriv.index = m.index;
          this.deriv.funding = m.funding;
          this.deriv.nextFunding = m.nextFunding;
          this.deriv.markAt = m.t;
          break;
        case 'liq':
          this.o.publish('liq', m);
          break;
        default:
          break;
      }
    }
  }

  /**
   * ID-aware BBO check: a bookTicker with update id u is compared only with the local book state right
   * after the diff whose final id is exactly u (same exchange state). Other ids are skipped, never guessed.
   */
  private checkBbo(): void {
    const v = this.status.verify.bbo;
    if (!this.bookHistory.length) return;
    const newest = this.bookHistory[this.bookHistory.length - 1].u;
    const keep: typeof this.pendingBbo = [];
    for (const b of this.pendingBbo) {
      if (b.u > newest) {
        keep.push(b);
        continue;
      }
      const h = this.bookHistory.find((x) => x.u === b.u);
      if (!h) continue; // bookTicker id inside a batched diff: no exact counterpart
      v.checks++;
      const tol = this.meta.tickSize / 2;
      if (Math.abs(h.bb - b.bid) <= tol && Math.abs(h.ba - b.ask) <= tol) {
        v.matches++;
        this.bboMismatchRun = 0;
      } else {
        v.mismatches++;
        this.bboMismatchRun++;
        v.lastMismatch = `u=${b.u} book ${h.bb}/${h.ba} vs bookTicker ${b.bid}/${b.ask}`;
        if (this.bboMismatchRun >= 3) {
          this.bboMismatchRun = 0;
          this.engine.beginSync();
          this.onGap('local book disagrees with bookTicker at the same update id (3×)');
        }
      }
    }
    this.pendingBbo = keep;
    v.pending = keep.length;
  }

  private noteLatency(now: number, et: number): void {
    if (et > 0) {
      this.latency.update(now - et);
      this.status.lastEventTime = et;
    }
  }

  private onTrade(tr: Trade): void {
    // aggTrade ids are consecutive per symbol: a jump means messages were lost
    if (tr.id !== undefined) {
      if (this.lastTradeId && tr.id <= this.lastTradeId) return; // duplicate / replayed message
      if (this.lastTradeId && tr.id > this.lastTradeId + 1) void this.fillTradeGap(this.lastTradeId + 1, tr.id - 1);
      this.lastTradeId = tr.id;
      this.status.verify.trades.lastId = tr.id;
    }
    this.engine.onTrade(tr);
    this.o.recorder?.trade(tr);
    this.o.persist?.onTrade(tr);
    const bt = Math.floor(tr.t / 60_000) * 60_000;
    if (!this.firstLiveBar) this.firstLiveBar = bt;
    let c = this.liveBars.get(bt);
    if (!c) {
      // the previous minute is closed: persist it if it was observed from its start without trade-id gaps
      const prev = this.liveBars.get(bt - 60_000);
      if (prev && prev.t > this.firstLiveBar && !this.gapMinutes.has(prev.t)) this.o.persist?.onCandle(prev, 'live');
      this.liveBars.set(bt, (c = { t: bt, o: tr.price, h: tr.price, l: tr.price, c: tr.price, v: 0, bv: 0, n: 0 }));
    }
    c.h = Math.max(c.h, tr.price);
    c.l = Math.min(c.l, tr.price);
    c.c = tr.price;
    c.v += tr.qty;
    if (tr.side === 1) c.bv += tr.qty;
    c.n = (c.n ?? 0) + 1;
    if (this.liveBars.size > 30) this.liveBars.delete(this.liveBars.keys().next().value!);
    this.tradeBatch.push([tr.t, tr.price, tr.qty, tr.side, tr.id ?? 0]);
    if (this.tradeBatch.length > 5000) this.flushTrades();
  }

  private async fillTradeGap(fromId: number, toId: number): Promise<void> {
    const v = this.status.verify.trades;
    v.idGaps++;
    v.missingIds += toId - fromId + 1;
    this.gapMinutes.add(Math.floor(Date.now() / 60_000) * 60_000);
    this.o.persist?.onGap(Date.now(), `aggTrade ids ${fromId}..${toId} missing on the live stream`);
    this.log(`[${this.key}] trade id gap ${fromId}..${toId}`);
    if (!this.adapter.fetchAggTradesFromId || this.o.skipWarmup) return;
    try {
      const got = (await this.adapter.fetchAggTradesFromId(this.meta.symbol, fromId, Math.min(1000, toId - fromId + 1))).filter((t) => t.id !== undefined && t.id <= toId);
      for (const t of got) this.o.recorder?.trade(t); // local history only; the live detectors never see late trades
      v.backfilled += got.length;
    } catch (e) {
      this.log(`[${this.key}] trade gap backfill failed: ${(e as Error).message}`);
    }
  }

  /** Compare a sample of live trades and closed live-built 1m bars with the exchange REST API. */
  private async verifyAgainstRest(): Promise<void> {
    const vt = this.status.verify.trades;
    try {
      if (this.adapter.fetchAggTradesFromId && this.lastTradeId > 200) {
        const fromId = this.lastTradeId - 150;
        const rest = await this.adapter.fetchAggTradesFromId(this.meta.symbol, fromId, 100);
        const local = new Map(this.o.recorder ? this.o.recorder.recentTrades().map((t) => [t.id, t]) : []);
        for (const r of rest) {
          const l = local.get(r.id);
          if (!l) continue;
          vt.restSamples++;
          if (l.price === r.price && l.qty === r.qty && l.side === r.side && l.t === r.t) vt.restMatches++;
          else vt.restMismatch = `id ${r.id}: live ${l.price}/${l.qty}/${l.side}/${l.t} vs REST ${r.price}/${r.qty}/${r.side}/${r.t}`;
        }
        vt.lastCheckAt = Date.now();
      }
      const vc = this.status.verify.candles;
      const k = await this.adapter.fetchKlines(this.meta.symbol, '1m', 6);
      for (const c of k.slice(0, -1)) {
        this.o.persist?.onCandle(c, 'rest');
        this.o.recorder?.candle1m(c);
        const l = this.liveBars.get(c.t);
        if (!l || c.t <= this.firstLiveBar) continue; // first bar only partially observed
        vc.compared++;
        const same = l.o === c.o && l.h === c.h && l.l === c.l && l.c === c.c;
        if (same) vc.ohlcMatches++;
        const vr = c.v > 0 ? Math.abs(l.v - c.v) / c.v : 0;
        if (vr < 1e-6) vc.volumeWithin++;
        if (!same || vr >= 1e-6) vc.lastDiff = `${new Date(c.t).toISOString()} live O${l.o} H${l.h} L${l.l} C${l.c} V${l.v.toFixed(6)} vs REST O${c.o} H${c.h} L${c.l} C${c.c} V${c.v}`;
        this.liveBars.delete(c.t);
      }
      vc.lastCheckAt = Date.now();
    } catch (e) {
      this.log(`[${this.key}] REST verification failed: ${(e as Error).message}`);
    }
  }

  private logVerification(): void {
    const s = this.status;
    this.log(
      'OFT_VERIFY ' +
        JSON.stringify({
          key: this.key,
          state: s.state,
          gate: s.gate,
          gateReason: s.gateReason,
          upSec: Math.round((Date.now() - s.since) / 1000),
          depthUpdates: s.depthUpdates,
          trades: s.trades,
          gaps: s.gaps,
          resyncs: s.resyncs,
          reconnects: s.reconnects,
          latencyMs: s.latencyMs,
          streams: Object.fromEntries(Object.entries(s.streams).map(([k, v]) => [k, { c: v.connected, msgs: v.messages, ageMs: v.lastMsgAt ? Date.now() - v.lastMsgAt : null, url: v.url }])),
          verify: s.verify,
          persist: this.o.persist?.getStatus(),
          rest: restHealth,
          mark: this.deriv.mark,
          book: { bb: this.engine.book.bestBid, ba: this.engine.book.bestAsk, levels: this.engine.book.size },
        }),
    );
  }

  private onEvent(ev: MarketEvent): void {
    this.o.recorder?.event(ev);
    this.o.persist?.onEvent(ev);
    this.o.publish('event', ev);
  }

  private onHeat(c: HeatColumn): void {
    this.o.recorder?.heat(c);
    this.o.persist?.onHeat(c);
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
    this.o.publish('book', { t: this.engine.book.lastT, u: this.engine.book.lastUpdateId, bids: top.bids, asks: top.asks, stats: this.engine.stats, bbo: this.lastBbo, reliable: [this.engine.book.reliableLo * this.meta.tickSize, this.engine.book.reliableHi * this.meta.tickSize] });
  }

  private tick(): void {
    const now = Date.now();
    if (this.stopped) return;
    const staleMs = this.o.staleMs ?? this.cfg.staleMs;
    const synced = this.engine.sync.state === 'synced' && this.status.synced;
    const stale = synced && now - this.lastDiffWall > staleMs;
    if (stale) {
      if (!this.staleSince) {
        this.staleSince = now;
        this.o.persist?.onGap(now, 'depth stale');
        this.setState('stale', `no depth update for ${((now - this.lastDiffWall) / 1000).toFixed(1)}s`);
        this.feedEvent('Данные устарели', `Нет обновлений стакана более ${staleMs / 1000} с. Новые сигналы заблокированы.`);
      } else if (now - this.staleSince > 30_000) {
        this.staleSince = now;
        this.log(`[${this.key}] stale for 30s, forcing reconnect`);
        this.depthSocket()?.restart();
      }
    } else if (this.staleSince && synced) {
      this.staleSince = 0;
      // a stale period is not continuous: rebuild from a fresh snapshot rather than trust the old state
      this.engine.beginSync();
      this.scheduleResync(10);
    }
    this.updateGate();
    this.engine.onTimer(now);
  }

  private everySecond(): void {
    const now = Date.now();
    this.status.latencyMs = Math.round(this.latency.value);
    this.status.depthLevels = this.engine.book.size;
    this.status.dropped = this.engine.sync.dropped;
    this.publishStatus();
    this.o.publish('large', { thr: this.engine.large.thr, list: this.engine.large.list(now) });
    this.o.publish('clusters', { list: this.engine.clusters.list(), vacuums: this.engine.clusters.vacuums });
    this.o.publish('ice', this.engine.iceberg.candidates(now));
    this.publishDeriv();
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
    this.o.publish('status', { ...this.status, heatStep: this.engine?.opts.heatStep, cvd: this.engine?.sessionCvd, atr1m: this.engine?.ctx.atr1m, persist: this.o.persist?.getStatus(), rest: restHealth });
  }

  private publishDeriv(): void {
    if (this.adapter.caps.markPrice || this.adapter.caps.openInterest) this.o.publish('deriv', this.deriv);
  }

  setConfig(cfg: DetectorConfig): void {
    this.cfg = cfg;
    this.engine.setConfig(cfg);
  }

  getStatus(): FeedStatus & { gate: boolean } {
    return this.status;
  }

  async stop(): Promise<void> {
    this.stopped = true;
    for (const t of this.timers) clearInterval(t);
    if (this.resyncTimer) clearTimeout(this.resyncTimer);
    for (const ws of this.sockets.values()) ws.stop();
    try {
      this.o.recorder?.flush(Date.now());
    } catch {
      /* ignore on shutdown */
    }
    if (this.o.persist) {
      const r = await this.o.persist.shutdown(8000);
      this.log(`[${this.key}] shutdown flush: unsaved rows ${r.unsavedRows}, unsaved archive blocks ${r.unsavedBlocks}`);
    }
  }
}
