import { startPg, FakeArchive } from './fixtures/pg.js';
import { PgRepo } from '../src/server/persist/repo.js';
import { PersistWriter, type WriterOptions } from '../src/server/persist/writer.js';
import { decodeBlock, replayBlock } from '../src/core/archiveCodec.js';
import { harness, TEST_META } from './fixtures/sim.js';
import type { HeatColumn, MarketEvent } from '../src/core/types.js';
import { RetentionService } from '../src/server/services/retention.js';

const OPTS: WriterOptions = { blockMs: 2000, bandFrac: 0.05, flushMs: 1_000_000, maxQueueRows: 50_000, maxPendingBlockBytes: 1 << 26, archive: true };
const ev = (id: string, t: number, status: string, conf: number): MarketEvent => ({ id, t, kind: 'iceberg', title: 'Предполагаемый айсберг', side: 'bid', price: 100, confidence: conf, explain: 'x', source: 'binance-futures', symbol: 'TESTUSDT', status });

let pg: Awaited<ReturnType<typeof startPg>>;
let repo: PgRepo;
beforeAll(async () => {
  pg = await startPg();
  repo = new PgRepo(pg.sql);
}, 60_000);
afterAll(async () => pg?.stop());

describe('Supabase repository on real Postgres', () => {
  it('candle upserts are idempotent', async () => {
    const c = { t: 60_000, o: 1, h: 2, l: 0.5, c: 1.5, v: 10, bv: 6, n: 3 };
    await repo.upsertCandles('binance-futures', 'AAA', [{ c, origin: 'rest' }]);
    await repo.upsertCandles('binance-futures', 'AAA', [{ c: { ...c, c: 1.7 }, origin: 'rest' }, { c: { ...c, t: 120_000 }, origin: 'live' }]);
    const rows = await repo.candles('binance-futures', 'AAA', 0, 1e12);
    expect(rows).toHaveLength(2);
    expect(rows[0].c).toBe(1.7);
  });

  it('events keep one current row + a status history; asOf returns the state known at that time (no look-ahead)', async () => {
    await repo.upsertEvents([{ ev: ev('e1', 1000, 'active', 60), detectedAt: 1500, algo: 'v' }]);
    await repo.upsertEvents([
      { ev: { ...ev('e1', 1000, 'active', 70), endT: 3000 }, detectedAt: 1500, algo: 'v' },
      { ev: { ...ev('e1', 1000, 'broken', 70), endT: 5000 }, detectedAt: 1500, algo: 'v' },
    ]);
    const cur = await repo.events('binance-futures', 'TESTUSDT', 0, 1e12, 10);
    expect(cur).toHaveLength(1);
    expect(cur[0].status).toBe('broken');
    const at2000 = await repo.events('binance-futures', 'TESTUSDT', 0, 1e12, 10, 2000);
    expect(at2000[0].status).toBe('active');
    expect(at2000[0].confidence).toBe(60);
    const at4000 = await repo.events('binance-futures', 'TESTUSDT', 0, 1e12, 10, 4000);
    expect(at4000[0].confidence).toBe(70);
    expect(at4000[0].status).toBe('active');
    expect(await repo.events('binance-futures', 'TESTUSDT', 0, 1e12, 10, 1200)).toHaveLength(0); // not yet detected
  });

  it('stores instrument parameters for use while the exchange REST is unavailable', async () => {
    const meta = { source: 'binance-futures', symbol: 'AAAUSDT', base: 'AAA', quote: 'USDT', tickSize: 0.1, stepSize: 0.001, pricePrecision: 1, qtyPrecision: 3 };
    await repo.putInstrument(meta);
    await repo.putInstrument({ ...meta, tickSize: 0.01, pricePrecision: 2 } as typeof meta);
    expect(await repo.getInstrument('binance-futures', 'AAAUSDT')).toEqual({ ...meta, tickSize: 0.01, pricePrecision: 2 });
    expect(await repo.getInstrument('binance-futures', 'NOPE')).toBeUndefined();
    expect((await repo.listInstruments<{ symbol: string }>('binance-futures')).map((m) => m.symbol)).toContain('AAAUSDT');
  });

  it('heat tiles round-trip and settings / record list / paper persist', async () => {
    const col: HeatColumn = { t: 10_000, dt: 10_000, p0: 99, step: 0.5, n: 2, bids: [1, -1], asks: [0, 2], exec: [], add: [], rem: [], bb: 99, ba: 99.5, hi: 0, lo: 0, last: 99.2 };
    await repo.upsertHeat('binance-futures', 'AAA', 10, [col]);
    await repo.upsertHeat('binance-futures', 'AAA', 10, [col]);
    expect(await repo.heat('binance-futures', 'AAA', 10, 0, 1e12, 10)).toEqual([col]);
    await repo.setRecord('binance-futures', 'BTCUSDT', true);
    await repo.setRecord('binance-futures', 'BTCUSDT', false);
    expect(await repo.recordList()).toEqual([{ source: 'binance-futures', symbol: 'BTCUSDT', enabled: false }]);
    await repo.setPaper('default', { open: [1] });
    expect(await repo.getPaper('default')).toEqual({ open: [1] });
    expect(await repo.logAlert({ t: 1, ruleId: 1, source: 's', symbol: 'X', eventId: 'e', body: {} })).toBe(true);
    expect(await repo.logAlert({ t: 2, ruleId: 1, source: 's', symbol: 'X', eventId: 'e', body: {} })).toBe(false); // idempotent
  });
});

describe('Persist writer: queue, tiles, archive blocks, recovery', () => {
  it('writes heat tiles, events, gaps and a verifiable L2 archive that replays to the live book', async () => {
    const h = harness();
    const arch = new FakeArchive();
    const w = new PersistWriter(TEST_META, repo, arch, () => {}, OPTS);
    const cols: HeatColumn[] = [];
    (h.engine as unknown as { sink: { heat: (c: HeatColumn) => void } }).sink.heat = (c) => (cols.push(c), w.onHeat(c));
    w.onSynced(h.engine.book, h.sim.t);
    const origDiff = h.engine.onDiff.bind(h.engine);
    h.engine.onDiff = (d) => {
      const ok = origDiff(d);
      if (ok) w.onDiff(d, h.engine.book);
      return ok;
    };
    for (let i = 0; i < 25; i++) {
      const tr = h.sim.trade(100, 0.5, -1);
      h.engine.onTrade(tr);
      w.onTrade(tr);
      if (h.sim.qty('bid', 100) <= 0) h.sim.set('bid', 100, 3);
      h.sim.advance(1000);
      h.engine.onDiff(h.sim.diff());
      h.engine.onTimer(h.sim.t);
    }
    w.onEvent(ev('w1', h.sim.t, 'active', 50));
    w.onGap(h.sim.t, 'test gap');
    await w.flush(true);
    const st = w.getStatus();
    expect(st.lastError).toBe('');
    expect(st.archive.blocks).toBeGreaterThanOrEqual(2);
    const ms = await repo.manifests(TEST_META.source, TEST_META.symbol, 0, 1e15);
    expect(ms.every((m) => m.status === 'final')).toBe(true);
    // replay the last block: its end state equals the live local book inside the band
    const last = ms[ms.length - 1];
    const blk = decodeBlock(arch.objects.get(last.path)!, last.sha256);
    const r = replayBlock(blk);
    expect(r.continuous).toBe(true);
    for (const [k, q] of h.engine.book.bids) if (k >= blk.loTick && k <= blk.hiTick) expect(r.book.bids.get(k)).toBe(q);
    expect(blk.trades.length).toBeGreaterThan(0);
    const tiles10 = await repo.heat(TEST_META.source, TEST_META.symbol, 10, 0, 1e15, 100);
    expect(tiles10.length).toBeGreaterThanOrEqual(1);
    expect(tiles10[0].dt).toBeGreaterThan(5000); // observed duration kept, not assumed
    expect((await repo.gaps(TEST_META.source, TEST_META.symbol, 0, 1e15)).some((g) => g.reason === 'test gap')).toBe(true);
    expect((await repo.events(TEST_META.source, TEST_META.symbol, 0, 1e15, 10)).some((e) => e.id === 'w1')).toBe(true);
  });

  it('keeps data queued and retries when storage fails; only acknowledged blocks are final', async () => {
    const h = harness();
    const arch = new FakeArchive();
    arch.failPuts = 1;
    const w = new PersistWriter({ ...TEST_META, symbol: 'RETRYUSDT' }, repo, arch, () => {}, OPTS);
    w.onSynced(h.engine.book, h.sim.t);
    for (let i = 0; i < 3; i++) {
      h.sim.set('bid', 99.9, 4 + i);
      h.sim.advance(1500);
      const d = h.sim.diff();
      h.engine.onDiff(d);
      w.onDiff(d, h.engine.book);
    }
    w.onGap(h.sim.t, 'close block');
    await w.flush(true);
    expect(w.getStatus().lastError).toMatch(/storage unavailable/);
    expect(w.getStatus().archive.pending).toBeGreaterThan(0);
    const pending = await repo.manifests('binance-futures', 'RETRYUSDT', 0, 1e15);
    expect(pending).toHaveLength(0); // manifests() lists final only
    await w.flush(true);
    expect(w.getStatus().archive.pending).toBe(0);
    expect((await repo.manifests('binance-futures', 'RETRYUSDT', 0, 1e15)).length).toBeGreaterThan(0);
  });

  it('after a crash, pending manifests are finalized if the object exists, otherwise removed and marked as a gap', async () => {
    const arch = new FakeArchive();
    const base = { source: 'binance-futures', symbol: 'CRASHUSDT', kind: 'l2', schema_version: 1, first_update_id: 1, last_update_id: 2, first_trade_id: null, last_trade_id: null, diffs: 1, trades: 0, depth_band: 0.02, sha256: 'x', status: 'pending' as const };
    await repo.upsertManifest({ ...base, path: 'binance-futures/CRASHUSDT/a.gz', t0: 1, t1: 2, bytes: 3 });
    await repo.upsertManifest({ ...base, path: 'binance-futures/CRASHUSDT/b.gz', t0: 3, t1: 4, bytes: 3 });
    arch.objects.set('binance-futures/CRASHUSDT/a.gz', new Uint8Array([1, 2, 3]));
    await pg.sql`update oft.archives set created_at = now() - interval '1 hour' where symbol='CRASHUSDT'`;
    const w = new PersistWriter({ ...TEST_META, symbol: 'CRASHUSDT' }, repo, arch, () => {}, OPTS);
    w.start();
    await new Promise((r) => setTimeout(r, 300));
    const fin = await repo.manifests('binance-futures', 'CRASHUSDT', 0, 1e15);
    expect(fin.map((m) => m.path)).toEqual(['binance-futures/CRASHUSDT/a.gz']);
    expect((await repo.gaps('binance-futures', 'CRASHUSDT', 0, 1e15))[0].reason).toMatch(/unfinished archive upload/);
    await w.shutdown(100);
  });

  it('keeps rows queued while Supabase is unreachable and reports the unsaved tail', async () => {
    let fail = true;
    const flaky = new Proxy(repo, {
      get(t, p) {
        const v = (t as unknown as Record<string | symbol, unknown>)[p];
        if (typeof v !== 'function') return v;
        return (...a: unknown[]) => (fail ? Promise.reject(new Error('connection refused (test)')) : (v as (...x: unknown[]) => unknown).apply(t, a));
      },
    });
    const w = new PersistWriter({ ...TEST_META, symbol: 'FLAKYUSDT' }, flaky, null, () => {}, { ...OPTS, archive: false });
    w.onCandle({ t: 60_000, o: 1, h: 1, l: 1, c: 1, v: 1, bv: 1 }, 'rest');
    await w.flush(true);
    expect(w.getStatus().queued).toBe(1);
    expect(w.getStatus().lastError).toMatch(/connection refused/);
    fail = false;
    await w.flush(true);
    expect(w.getStatus().queued).toBe(0);
    expect(await repo.candles('binance-futures', 'FLAKYUSDT', 0, 1e12)).toHaveLength(1);
  });
});

describe('Retention and quota guard', () => {
  it('purges by policy, deletes expired archive objects and pauses detailed writes over budget', async () => {
    const arch = new FakeArchive();
    await repo.setRecord('binance-futures', 'OLDUSDT', true);
    const base = { source: 'binance-futures', symbol: 'OLDUSDT', kind: 'l2', schema_version: 1, first_update_id: 1, last_update_id: 2, first_trade_id: null, last_trade_id: null, diffs: 1, trades: 0, depth_band: 0.02, sha256: 'x', status: 'pending' as const, bytes: 3 };
    await repo.upsertManifest({ ...base, path: 'old/a.gz', t0: 1, t1: 2 });
    await repo.finalizeManifest('old/a.gz');
    arch.objects.set('old/a.gz', new Uint8Array(3));
    let policy: { archive: boolean; heat10: boolean } | null = null;
    const r = new RetentionService(repo, arch, { heat10Ms: 1, heat60Ms: 1, eventsMs: 1e15, candlesMs: 1e15, archiveMs: 1000, dbBudgetBytes: 1, storageBudgetBytes: 1e12 }, () => {}, (p) => (policy = p));
    await r.run(Date.now());
    expect(arch.objects.has('old/a.gz')).toBe(false);
    expect(r.status.lastPurge.archives).toBe(1);
    expect(r.status.dbBytes).toBeGreaterThan(0);
    expect(r.status.heat10Paused).toBe(true); // tiny budget => pause 10 s tiles, never silent overrun
    expect(policy).toEqual({ archive: true, heat10: false });
  });
});
