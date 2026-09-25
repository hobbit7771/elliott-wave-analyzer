// Retention + quota guard + archive integrity check (main thread, periodic).
import type { Repo, ArchiveManifest } from '../persist/repo.js';
import type { ArchiveClient } from '../persist/archive.js';
import { decodeBlock, replayBlock } from '../../core/archiveCodec.js';

export interface RetentionPolicy {
  heat10Ms: number;
  heat60Ms: number;
  eventsMs: number;
  candlesMs: number;
  archiveMs: number;
  dbBudgetBytes: number; // pause detailed writes above this
  storageBudgetBytes: number;
}

const H = 3600_000;
const D = 24 * H;

export function policyFromEnv(env = process.env): RetentionPolicy {
  return {
    heat10Ms: +(env.RET_HEAT10_MS ?? 2 * D),
    heat60Ms: +(env.RET_HEAT60_MS ?? 30 * D),
    eventsMs: +(env.RET_EVENTS_MS ?? 10 * D),
    candlesMs: +(env.RET_CANDLES_MS ?? 365 * D),
    archiveMs: +(env.RET_ARCHIVE_MS ?? 12 * H),
    // Supabase Free: 500 MB database / 1 GB storage (verify on supabase.com/pricing); keep headroom
    dbBudgetBytes: +(env.DB_BUDGET_BYTES ?? 400 * 1024 * 1024),
    storageBudgetBytes: +(env.STORAGE_BUDGET_BYTES ?? 850 * 1024 * 1024),
  };
}

export interface RetentionStatus {
  policy: RetentionPolicy;
  lastRunAt: number;
  lastPurge: Record<string, number>;
  dbBytes: number;
  tables: Record<string, number>;
  storage: { objects: number; bytes: number } | null;
  archivePaused: boolean;
  heat10Paused: boolean;
  lastError: string;
  lastArchiveCheck: { path: string; next: string; ok: boolean; detail: string; at: number } | null;
  growth: { at: number; dbBytes: number; storageBytes: number }[];
}

export class RetentionService {
  status: RetentionStatus;
  constructor(
    private repo: Repo,
    private archive: ArchiveClient | null,
    policy: RetentionPolicy,
    private log: (m: string) => void,
    private onPolicy: (p: { archive: boolean; heat10: boolean }) => void,
  ) {
    this.status = { policy, lastRunAt: 0, lastPurge: {}, dbBytes: 0, tables: {}, storage: null, archivePaused: false, heat10Paused: false, lastError: '', lastArchiveCheck: null, growth: [] };
  }

  async run(now = Date.now()): Promise<void> {
    const p = this.status.policy;
    try {
      // 1. aggregates are written live (tiles, candles, events) before raw blocks expire, so deleting
      //    raw archive blocks never removes the only copy of the aggregated view
      this.status.lastPurge = await this.repo.purge(p, now);
      if (this.archive) {
        const expired = await this.expiredArchives(now - p.archiveMs);
        for (const m of expired) {
          await this.archive.del(m.path).catch(() => {});
          await this.repo.deleteManifest(m.path);
        }
        this.status.lastPurge.archives = expired.length;
        this.status.storage = await this.archive.usage();
      }
      const sz = await this.repo.sizes();
      this.status.dbBytes = sz.dbBytes;
      this.status.tables = sz.tables;
      this.status.growth.push({ at: now, dbBytes: sz.dbBytes, storageBytes: this.status.storage?.bytes ?? 0 });
      if (this.status.growth.length > 200) this.status.growth.shift();
      // 2. quota guard: pause the heaviest detailed writes instead of silently exceeding the free plan
      const archivePaused = !!this.status.storage && this.status.storage.bytes > p.storageBudgetBytes;
      const heat10Paused = sz.dbBytes > p.dbBudgetBytes;
      // over the DB budget: the detector journal is thinned first (events older than half their retention)
      if (heat10Paused) this.status.lastPurge.eventsOverBudget = await this.repo.purge({ ...p, heat10Ms: p.heat10Ms, eventsMs: p.eventsMs / 2 }, now).then((r) => r.events);
      if (archivePaused !== this.status.archivePaused || heat10Paused !== this.status.heat10Paused) {
        this.status.archivePaused = archivePaused;
        this.status.heat10Paused = heat10Paused;
        this.log(`retention: archive ${archivePaused ? 'PAUSED (storage budget reached)' : 'active'}, heat10 ${heat10Paused ? 'PAUSED (db budget reached)' : 'active'}`);
        this.onPolicy({ archive: !archivePaused, heat10: !heat10Paused });
      }
      this.status.lastError = '';
    } catch (e) {
      this.status.lastError = (e as Error).message;
      this.log(`retention failed: ${this.status.lastError}`);
    }
    this.status.lastRunAt = now;
    this.log('OFT_STORAGE ' + JSON.stringify({ dbBytes: this.status.dbBytes, tables: this.status.tables, storage: this.status.storage, purge: this.status.lastPurge, archivePaused: this.status.archivePaused, heat10Paused: this.status.heat10Paused }));
  }

  private async expiredArchives(before: number): Promise<ArchiveManifest[]> {
    const syms = await this.repo.recordList();
    const out: ArchiveManifest[] = [];
    for (const s of syms) out.push(...(await this.repo.manifests(s.source, s.symbol, 0, before)).filter((m) => m.t1 < before));
    return out;
  }

  /**
   * Integrity check: replay the newest finished block and compare its final book (inside the band)
   * with the snapshot that opens the directly following block (same update id => same exchange state).
   */
  async verifyArchive(source: string, symbol: string, futures: boolean): Promise<void> {
    if (!this.archive) return;
    try {
      const ms = await this.repo.manifests(source, symbol, Date.now() - 6 * H, Date.now());
      for (let i = ms.length - 2; i >= 0; i--) {
        const a = ms[i];
        const b = ms[i + 1];
        if (a.last_update_id === null || b.first_update_id !== a.last_update_id) continue; // not contiguous
        const blockA = decodeBlock(await this.archive.get(a.path), a.sha256);
        const blockB = decodeBlock(await this.archive.get(b.path), b.sha256);
        const r = replayBlock(blockA, Infinity, futures);
        const lo = Math.max(blockA.loTick, blockB.loTick);
        const hi = Math.min(blockA.hiTick, blockB.hiTick);
        let compared = 0;
        let diff = 0;
        const expect = new Map<string, number>();
        for (const [k, q] of blockB.snapshot.bids) if (k >= lo && k <= hi) expect.set('b' + k, q);
        for (const [k, q] of blockB.snapshot.asks) if (k >= lo && k <= hi) expect.set('a' + k, q);
        const got = new Map<string, number>();
        for (const [k, q] of r.book.bids) if (k >= lo && k <= hi) got.set('b' + k, q);
        for (const [k, q] of r.book.asks) if (k >= lo && k <= hi) got.set('a' + k, q);
        for (const k of new Set([...expect.keys(), ...got.keys()])) {
          compared++;
          if ((expect.get(k) ?? 0) !== (got.get(k) ?? 0)) diff++;
        }
        const ok = r.continuous && r.lastUpdateId === blockB.snapshot.lastUpdateId && diff === 0;
        this.status.lastArchiveCheck = { path: a.path, next: b.path, ok, detail: `continuous=${r.continuous} diffs=${r.appliedDiffs} levelsCompared=${compared} mismatched=${diff} band=[${lo},${hi}]`, at: Date.now() };
        this.log('OFT_ARCHIVE_VERIFY ' + JSON.stringify(this.status.lastArchiveCheck));
        return;
      }
    } catch (e) {
      this.log(`archive verify failed: ${(e as Error).message}`);
    }
  }
}
