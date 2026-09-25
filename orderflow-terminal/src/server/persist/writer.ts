// Durable writer for one instrument: bounded queues, batched idempotent upserts with retry/backoff,
// 10 s / 60 s heatmap tiles, raw L2 archive blocks (manifest pending -> upload -> final), gaps and coverage.
// Nothing is reported as saved until the database / storage acknowledged it.
import type { Candle, DepthDiff, HeatColumn, InstrumentMeta, MarketEvent, Trade } from '../../core/types.js';
import type { OrderBook } from '../../core/orderbook.js';
import { downsample } from '../../core/heatmap.js';
import { ARCHIVE_SCHEMA_VERSION, BlockBuilder, encodeBlock } from '../../core/archiveCodec.js';
import { backoffDelay } from '../ingestion/wsClient.js';
import type { ArchiveClient } from './archive.js';
import type { GapRow, Repo } from './repo.js';

export const ALGO_VERSION = 'oft-detectors-4';

export interface PersistStatus {
  enabled: boolean;
  archiveEnabled: boolean;
  lastOkAt: number;
  lastError: string;
  lastErrorAt: number;
  queued: number;
  queuedBytes: number;
  dropped: number;
  oldestUnsavedMs: number;
  savedRows: number;
  archive: { blocks: number; bytes: number; lastPath: string; lastAt: number; pending: number; failed: number; lastBlockMs: number };
}

export interface WriterOptions {
  blockMs: number; // archive block length
  bandFrac: number; // archive price band around mid (fraction)
  flushMs: number;
  maxQueueRows: number;
  maxPendingBlockBytes: number;
  archive: boolean;
}

export const DEFAULT_WRITER: WriterOptions = {
  blockMs: +(process.env.ARCHIVE_BLOCK_MS ?? 300_000),
  bandFrac: +(process.env.ARCHIVE_BAND ?? 0.02),
  flushMs: 5000,
  maxQueueRows: 20_000,
  maxPendingBlockBytes: 40 << 20,
  archive: (process.env.ARCHIVE_ENABLED ?? '1') !== '0',
};

interface Item {
  kind: 'candle' | 'heat10' | 'heat60' | 'event' | 'gap';
  t: number;
  data: unknown;
}

export class PersistWriter {
  status: PersistStatus;
  private q: Item[] = [];
  private blocks: { enc: ReturnType<typeof encodeBlock>; builder: BlockBuilder; attempts: number; firstFailAt: number }[] = [];
  private cur: BlockBuilder | null = null;
  private heat1: HeatColumn[] = [];
  private heat10: HeatColumn[] = [];
  private flushing = false;
  private failCount = 0;
  private nextTry = 0;
  private timer: NodeJS.Timeout | null = null;
  private openGap: GapRow | null = null;
  private covT0 = 0;
  private covT1 = 0;
  private lastTradeId = 0;
  private detectedAt = new Map<string, number>();

  /** configured = Supabase credentials exist; repo may attach later (reconnect) — until then rows queue up */
  private configured: boolean;

  constructor(
    private meta: InstrumentMeta,
    private repo: Repo | null,
    private archive: ArchiveClient | null,
    private log: (m: string) => void,
    private o: WriterOptions = DEFAULT_WRITER,
    configured = !!repo,
  ) {
    this.configured = configured;
    this.status = {
      enabled: configured,
      archiveEnabled: !!(configured && archive && o.archive),
      lastOkAt: 0,
      lastError: repo ? '' : 'Supabase not configured: history is NOT being written',
      lastErrorAt: 0,
      queued: 0,
      queuedBytes: 0,
      dropped: 0,
      oldestUnsavedMs: 0,
      savedRows: 0,
      archive: { blocks: 0, bytes: 0, lastPath: '', lastAt: 0, pending: 0, failed: 0, lastBlockMs: 0 },
    };
  }

  start(): void {
    if (!this.configured) return;
    this.timer = setInterval(() => void this.flush(), this.o.flushMs);
    if (this.repo) void this.recoverPending();
  }

  private heat10On = true;
  /** Quota guard from the main thread: pause archive blocks / 10 s tiles when budgets are reached. */
  setPolicy(p: { archive: boolean; heat10: boolean }): void {
    this.status.archiveEnabled = this.configured && !!this.archive && this.o.archive && p.archive;
    this.heat10On = p.heat10;
    if (!this.status.archiveEnabled) this.cur = null;
  }

  /** Late connection to Supabase: queued rows are flushed on the next cycle. */
  attach(repo: Repo): void {
    this.repo = repo;
    this.status.lastError = '';
    void this.recoverPending();
  }

  // ---------------- inputs ----------------

  /** A (re)synced book: start a new archive block from its exact current state. */
  onSynced(book: OrderBook, t: number): void {
    if (!this.firstSyncT) this.firstSyncT = t;
    this.closeBlock(t);
    if (this.openGap) {
      this.openGap.t1 = t;
      this.push({ kind: 'gap', t, data: { ...this.openGap } });
      this.openGap = null;
    }
    if (this.status.archiveEnabled && isFinite(book.bestBid) && isFinite(book.bestAsk)) this.cur = new BlockBuilder(this.meta.source, this.meta.symbol, this.meta.tickSize, book, t, this.o.bandFrac);
    this.covT0 = t;
    this.covT1 = t;
  }

  /** Continuity broken (gap, disconnect, stale): close the block and open a gap interval. */
  onGap(t: number, reason: string): void {
    this.closeBlock(t);
    this.flushCoverage();
    if (!this.openGap) {
      this.openGap = { source: this.meta.source, symbol: this.meta.symbol, stream: 'l2', t0: t, t1: null, reason };
      this.push({ kind: 'gap', t, data: { ...this.openGap } });
    }
  }

  onDiff(d: DepthDiff, book: OrderBook): void {
    if (!this.cur) return;
    this.cur.addDiff(d);
    this.covT1 = Math.max(this.covT1, d.t);
    if (d.t - this.cur.block.t0 >= this.o.blockMs) {
      this.closeBlock(d.t);
      if (this.status.archiveEnabled) this.cur = new BlockBuilder(this.meta.source, this.meta.symbol, this.meta.tickSize, book, d.t, this.o.bandFrac);
    }
  }

  onTrade(tr: Trade): void {
    // WebSocket trades are the only archive source (REST backfill never enters blocks) and ids dedupe replays
    if (tr.id !== undefined && tr.id <= this.lastTradeId) return;
    if (tr.id !== undefined) this.lastTradeId = tr.id;
    this.cur?.addTrade(tr);
  }

  onHeat(c: HeatColumn): void {
    this.heat1.push(c);
    const bucket = (t: number) => Math.floor((t - 1) / 10_000);
    if (this.heat1.length > 1 && bucket(c.t) !== bucket(this.heat1[0].t)) {
      const done = this.heat1.filter((x) => bucket(x.t) === bucket(this.heat1[0].t));
      this.heat1 = this.heat1.slice(done.length);
      for (const t10 of downsample(done, 10_000)) {
        if (this.heat10On) this.push({ kind: 'heat10', t: t10.t, data: t10 });
        this.heat10.push(t10);
      }
      const b60 = (t: number) => Math.floor((t - 1) / 60_000);
      if (this.heat10.length > 1 && b60(this.heat10[this.heat10.length - 1].t) !== b60(this.heat10[0].t)) {
        const d60 = this.heat10.filter((x) => b60(x.t) === b60(this.heat10[0].t));
        this.heat10 = this.heat10.slice(d60.length);
        for (const t60 of downsample(d60, 60_000)) this.push({ kind: 'heat60', t: t60.t, data: t60 });
      }
    }
  }

  onEvent(ev: MarketEvent): void {
    if (ev.kind === 'feed') return; // feed events go to gaps/coverage, not the detector journal
    if (!this.detectedAt.has(ev.id)) this.detectedAt.set(ev.id, Date.now());
    if (this.detectedAt.size > 20_000) this.detectedAt.delete(this.detectedAt.keys().next().value!);
    this.push({ kind: 'event', t: ev.t, data: { ev, detectedAt: this.detectedAt.get(ev.id)!, algo: ALGO_VERSION } });
  }

  onCandle(c: Candle, origin: 'live' | 'rest'): void {
    this.push({ kind: 'candle', t: c.t, data: { c, origin } });
  }

  private push(it: Item): void {
    if (!this.configured) return;
    this.q.push(it);
    if (this.q.length > this.o.maxQueueRows) {
      const drop = this.q.length - this.o.maxQueueRows;
      this.q.splice(0, drop);
      this.status.dropped += drop;
      this.status.lastError = `write queue overflow: ${drop} rows dropped (Supabase unreachable?)`;
      this.status.lastErrorAt = Date.now();
    }
  }

  private closeBlock(t: number): void {
    const b = this.cur;
    this.cur = null;
    if (!b || b.empty) return;
    b.block.t1 = Math.max(b.block.t1, t);
    const enc = encodeBlock(b.block);
    this.blocks.push({ enc, builder: b, attempts: 0, firstFailAt: 0 });
    let bytes = this.blocks.reduce((s, x) => s + x.enc.bytes.length, 0);
    while (bytes > this.o.maxPendingBlockBytes && this.blocks.length > 1) {
      const lost = this.blocks.shift()!;
      bytes -= lost.enc.bytes.length;
      this.status.archive.failed++;
      this.status.dropped += lost.builder.block.diffs.length + lost.builder.block.trades.length;
      this.push({ kind: 'gap', t: lost.builder.block.t0, data: { source: this.meta.source, symbol: this.meta.symbol, stream: 'archive', t0: lost.builder.block.t0, t1: lost.builder.block.t1, reason: 'archive upload failed; block dropped from memory' } });
    }
  }

  private flushCoverage(): void {
    if (this.covT0 && this.covT1 > this.covT0 && this.repo) {
      const [a, b] = [this.covT0, this.covT1];
      this.pendingCoverage = { t0: a, t1: b };
    }
  }
  private pendingCoverage: { t0: number; t1: number } | null = null;
  private firstSyncT = 0;
  private downtimeChecked = false;

  /** Once per process: the interval between the previous process's last recorded book and our first sync is a gap. */
  private async recordDowntime(): Promise<void> {
    if (this.downtimeChecked || !this.firstSyncT || !this.repo) return;
    const prev = (await this.repo.coverage(this.meta.source, this.meta.symbol)).filter((c) => c.stream === 'l2' && c.t0 < this.firstSyncT);
    const prevEnd = prev.reduce((m, c) => Math.max(m, Math.min(c.t1, this.firstSyncT)), 0);
    if (prevEnd && this.firstSyncT - prevEnd > 5000) {
      await this.repo.upsertGaps([
        { source: this.meta.source, symbol: this.meta.symbol, stream: 'l2', t0: prevEnd, t1: this.firstSyncT, reason: 'сервис не работал (перезапуск, деплой или сон Render Free) — данных за интервал нет' },
      ]);
      this.log(`[persist ${this.meta.symbol}] downtime gap recorded: ${new Date(prevEnd).toISOString()} .. ${new Date(this.firstSyncT).toISOString()}`);
    }
    this.downtimeChecked = true;
  }

  // ---------------- flushing ----------------

  async flush(force = false): Promise<void> {
    if (!this.repo) {
      this.updateStatus();
      return;
    }
    if (this.flushing) return;
    if (!force && Date.now() < this.nextTry) return;
    this.flushing = true;
    const batch = this.q.splice(0, 2000);
    try {
      const by = (k: Item['kind']) => batch.filter((x) => x.kind === k).map((x) => x.data);
      const src = this.meta.source;
      const sym = this.meta.symbol;
      await this.repo.upsertCandles(src, sym, dedupe(by('candle') as { c: Candle; origin: string }[], (x) => String(x.c.t)));
      await this.repo.upsertHeat(src, sym, 10, dedupe(by('heat10') as HeatColumn[], (x) => String(x.t)));
      await this.repo.upsertHeat(src, sym, 60, dedupe(by('heat60') as HeatColumn[], (x) => String(x.t)));
      await this.repo.upsertEvents(dedupe(by('event') as { ev: MarketEvent; detectedAt: number; algo: string }[], (x) => x.ev.id + '|' + (x.ev.status ?? '') + '|' + x.ev.confidence));
      await this.repo.upsertGaps(dedupe(by('gap') as GapRow[], (x) => x.stream + x.t0));
      if (this.covT0 && this.covT1 > this.covT0) await this.repo.upsertCoverage(src, sym, 'l2', this.covT0, this.covT1);
      if (this.pendingCoverage) {
        await this.repo.upsertCoverage(src, sym, 'l2', this.pendingCoverage.t0, this.pendingCoverage.t1);
        this.pendingCoverage = null;
      }
      await this.recordDowntime();
      this.status.savedRows += batch.length;
      await this.flushBlocks();
      this.failCount = 0;
      this.status.lastOkAt = Date.now();
    } catch (e) {
      this.q.unshift(...batch); // retry later, nothing is lost silently
      this.failCount++;
      this.nextTry = Date.now() + backoffDelay(this.failCount, 2000, 60_000);
      this.status.lastError = (e as Error).message.slice(0, 300);
      this.status.lastErrorAt = Date.now();
      this.log(`[persist ${this.meta.symbol}] write failed (#${this.failCount}): ${this.status.lastError}`);
    } finally {
      this.flushing = false;
      this.updateStatus();
    }
  }

  private async flushBlocks(): Promise<void> {
    if (!this.archive || !this.repo) return;
    while (this.blocks.length) {
      const it = this.blocks[0];
      const b = it.builder.block;
      const m = {
        path: it.enc.path,
        source: b.source,
        symbol: b.symbol,
        kind: 'l2',
        schema_version: ARCHIVE_SCHEMA_VERSION,
        t0: b.t0,
        t1: b.t1,
        first_update_id: b.snapshot.lastUpdateId,
        last_update_id: b.diffs.length ? b.diffs[b.diffs.length - 1][2] : b.snapshot.lastUpdateId,
        first_trade_id: b.trades.length ? b.trades[0][4] : null,
        last_trade_id: b.trades.length ? b.trades[b.trades.length - 1][4] : null,
        diffs: b.diffs.length,
        trades: b.trades.length,
        depth_band: this.o.bandFrac,
        bytes: it.enc.bytes.length,
        sha256: it.enc.sha256,
        status: 'pending' as const,
      };
      const t0 = Date.now();
      try {
        await this.repo.upsertManifest(m); // 1. pending manifest (never points to a missing final object)
        await this.archive.put(it.enc.path, it.enc.bytes); // 2. upload (idempotent: existing object is kept)
        const chk = await this.archive.exists(it.enc.path); // 3. verify
        if (!chk.exists || (chk.size !== null && chk.size !== it.enc.bytes.length)) throw new Error(`archive verify failed for ${it.enc.path}`);
        await this.repo.finalizeManifest(it.enc.path); // 4. final
      } catch (e) {
        it.attempts++;
        if (!it.firstFailAt) it.firstFailAt = Date.now();
        throw e;
      }
      this.blocks.shift();
      this.status.archive.blocks++;
      this.status.archive.bytes += it.enc.bytes.length;
      this.status.archive.lastPath = it.enc.path;
      this.status.archive.lastAt = Date.now();
      this.status.archive.lastBlockMs = Date.now() - t0;
      this.log(`[persist ${this.meta.symbol}] archived ${it.enc.path} (${(it.enc.bytes.length / 1024).toFixed(0)} KiB, ${m.diffs} diffs, ${m.trades} trades)`);
    }
  }

  /** On start: finish or remove manifests left pending by a crash (upload may or may not have happened). */
  private async recoverPending(): Promise<void> {
    if (!this.repo || !this.archive) return;
    try {
      const pend = (await this.repo.pendingManifests(120_000)).filter((m) => m.source === this.meta.source && m.symbol === this.meta.symbol);
      for (const m of pend) {
        const chk = await this.archive.exists(m.path);
        if (chk.exists && (chk.size === null || chk.size === m.bytes)) await this.repo.finalizeManifest(m.path);
        else {
          if (chk.exists) await this.archive.del(m.path);
          await this.repo.deleteManifest(m.path);
          await this.repo.upsertGaps([{ source: m.source, symbol: m.symbol, stream: 'archive', t0: m.t0, t1: m.t1, reason: 'unfinished archive upload removed after restart' }]);
        }
      }
      if (pend.length) this.log(`[persist ${this.meta.symbol}] recovered ${pend.length} pending archive manifests`);
    } catch (e) {
      this.log(`[persist ${this.meta.symbol}] pending recovery failed: ${(e as Error).message}`);
    }
  }

  private updateStatus(): void {
    const now = Date.now();
    this.status.queued = this.q.length + this.blocks.length;
    this.status.queuedBytes = this.blocks.reduce((s, x) => s + x.enc.bytes.length, 0);
    this.status.archive.pending = this.blocks.length;
    const oldest = Math.min(this.q.length ? this.q[0].t : now, this.blocks.length ? this.blocks[0].builder.block.t0 : now, this.cur ? this.cur.block.t0 : now);
    this.status.oldestUnsavedMs = Math.max(0, now - oldest);
  }

  getStatus(): PersistStatus {
    this.updateStatus();
    return this.status;
  }

  /** SIGTERM: close the open block and try to flush within the deadline. Reports what could not be saved. */
  async shutdown(deadlineMs = 8000): Promise<{ unsavedRows: number; unsavedBlocks: number }> {
    if (this.timer) clearInterval(this.timer);
    this.closeBlock(Date.now());
    this.onGap(Date.now(), 'server shutdown');
    const until = Date.now() + deadlineMs;
    while ((this.q.length || this.blocks.length) && Date.now() < until && this.repo) {
      await this.flush(true);
      if (this.failCount) break;
    }
    return { unsavedRows: this.q.length, unsavedBlocks: this.blocks.length };
  }
}

function dedupe<T>(xs: T[], key: (x: T) => string): T[] {
  const m = new Map<string, T>();
  for (const x of xs) m.set(key(x), x);
  return [...m.values()];
}
