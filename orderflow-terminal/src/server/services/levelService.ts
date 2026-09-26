// Strong D1 levels, level setups (historical walk-forward + live) and the real-time watchlist.
//
// Data: closed exchange klines (1d and 15m) are fetched from REST once and cached in Supabase (oft.klines),
// afterwards only the tail is fetched — so a restart or a REST ban does not lose the history.
// History: the walk-forward backtest runs in a worker thread (analysisWorker.ts).
// Live: a GerchikEngine per instrument is warmed up on the last days of CLOSED 15m bars and then fed
// every newly CLOSED 15m bar — the same engine class the backtest uses.
import { Worker } from 'node:worker_threads';
import type { Candle, SourceId } from '../../core/types.js';
import type { MarketAdapter } from '../adapters/adapter.js';
import type { Repo } from '../persist/repo.js';
import type { Setup } from '../../core/levelEngine/setupEngine.js';
import { GerchikEngine, SITE_GERCHIK_PARAMS, type GerchikParams } from '../../core/levelEngine/gerchik.js';
import type { DailyLevel } from '../../core/levelEngine/dailyLevels.js';

const M15 = 15 * 60_000;
const DAY = 86_400_000;

export interface HistoryResult {
  symbol: string;
  computedAt: number;
  ms: number;
  coverage: { from: number; to: number; bars: number; days: number };
  params: GerchikParams | Record<string, unknown>;
  chosenBy: string;
  grid: unknown[];
  segments: unknown[];
  overall: unknown;
  byDirection: unknown;
  byScore: unknown;
  byRegime: unknown;
  setups: Setup[];
}

interface Ctx {
  source: SourceId;
  symbol: string;
  key: string;
  tick: number;
  engine: GerchikEngine | null;
  params: GerchikParams;
  lastBarT: number;
  history: HistoryResult | null;
  status: string;
  error: string;
  live: Setup[];
  price: number;
  running: boolean;
}

export class LevelService {
  private ctx = new Map<string, Ctx>();
  private worker: Worker | null = null;
  private reqId = 0;
  private pending = new Map<number, (r: { ok: boolean; result?: HistoryResult; error?: string }) => void>();
  private queue: string[] = [];
  private busy = false;
  readonly historyDays = +(process.env.LEVELS_HISTORY_DAYS ?? 365);

  constructor(
    private deps: {
      adapter: (s: SourceId) => MarketAdapter;
      repo: () => Repo | null;
      log: (m: string) => void;
      workerPath: string;
      tickOf: (s: SourceId, sym: string) => Promise<number>;
      onLiveSetup: (s: Setup) => void;
    },
  ) {}

  keys(): string[] {
    return [...this.ctx.keys()];
  }

  /** Start tracking an instrument: live engine now, historical analysis queued. */
  async ensure(source: SourceId, symbol: string): Promise<void> {
    const key = `${source}:${symbol}`;
    if (this.ctx.has(key)) return;
    const c: Ctx = { source, symbol, key, tick: 0, engine: null, params: SITE_GERCHIK_PARAMS, lastBarT: 0, history: null, status: 'loading', error: '', live: [], price: NaN, running: false };
    this.ctx.set(key, c);
    const repo = this.deps.repo();
    const stored = repo ? await repo.getSetting<HistoryResult>('levelsetups:' + key).catch(() => undefined) : undefined;
    // results of an older engine (or older site parameters) are shown until the recomputation replaces them
    const current = stored && JSON.stringify(stored.params) === JSON.stringify(SITE_GERCHIK_PARAMS);
    if (stored) c.history = stored;
    try {
      c.tick = await this.deps.tickOf(source, symbol);
      await this.startLive(c);
    } catch (e) {
      c.status = 'error';
      c.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[levels ${key}] live start failed: ${c.error}`);
    }
    // a stored result younger than 12 h is reused; otherwise recompute in the background
    if (!stored || !current || Date.now() - stored.computedAt > 12 * 3600_000) this.enqueue(key);
  }

  forget(key: string): void {
    this.ctx.delete(key);
  }

  // ---------------- data ----------------

  /** Closed klines for [from, now], from the Supabase cache plus the missing tail from REST. */
  private async loadKlines(c: Ctx, tf: '15m' | '1d', from: number): Promise<Candle[]> {
    const repo = this.deps.repo();
    const ms = tf === '15m' ? M15 : DAY;
    const now = Date.now();
    const cached = repo ? await repo.klines(c.source, c.symbol, tf, from, now).catch(() => []) : [];
    const have = new Map(cached.map((k) => [k.t, k]));
    // how far back REST must go: the start of the period if the cache does not cover it, otherwise the
    // earliest hole in the cache (an interrupted earlier download), otherwise just the newest cached bar
    let target = cached.length ? cached[cached.length - 1].t : from;
    if (!cached.length || cached[0].t > from + ms) target = from;
    else for (let k = 1; k < cached.length; k++) if (cached[k].t - cached[k - 1].t > ms) {
      target = cached[k - 1].t;
      break;
    }
    const fetched: Candle[] = [];
    const ad = this.deps.adapter(c.source);
    let end: number | undefined;
    for (let page = 0; page < 60; page++) {
      let rows: Candle[];
      try {
        rows = await ad.fetchKlines(c.symbol, tf, 1500, end);
      } catch (e) {
        this.deps.log(`[levels ${c.key}] ${tf} klines fetch stopped: ${(e as Error).message.slice(0, 120)}`);
        break;
      }
      if (!rows.length) break;
      for (const r of rows) if (r.t + ms <= now) fetched.push(r); // CLOSED bars only
      if (rows[0].t <= target) break;
      end = rows[0].t - 1;
      await new Promise((r) => setTimeout(r, 250));
    }
    for (const f of fetched) have.set(f.t, f);
    if (repo && fetched.length) await repo.upsertKlines(c.source, c.symbol, tf, fetched).catch((e) => this.deps.log(`[levels ${c.key}] klines cache write failed: ${(e as Error).message}`));
    return [...have.values()].filter((k) => k.t >= from).sort((a, b) => a.t - b.t);
  }

  // ---------------- live ----------------

  private async startLive(c: Ctx): Promise<void> {
    const now = Date.now();
    const daily = await this.loadKlines(c, '1d', now - 1000 * DAY);
    const warm = await this.loadKlines(c, '15m', now - 7 * DAY);
    if (daily.length < 60 || !warm.length) throw new Error(`not enough history (D1 ${daily.length}, 15m ${warm.length})`);
    const dailyBefore = daily.filter((d) => d.t + DAY <= warm[0].t);
    const eng = new GerchikEngine({ symbol: c.symbol, exchange: c.source, tick: c.tick }, dailyBefore, c.params);
    for (const b of warm) eng.step(b);
    c.engine = eng;
    c.lastBarT = warm[warm.length - 1].t;
    c.price = warm[warm.length - 1].c;
    c.status = 'live';
    this.deps.log(`[levels ${c.key}] live engine ready: ${eng.levels.length} D1 levels, ${warm.length} warm-up 15m bars`);
  }

  /** Feed newly closed 15m bars to every live engine; refresh watchlist prices. Call every minute. */
  async tick(): Promise<void> {
    const bySource = new Map<SourceId, Record<string, number>>();
    for (const c of this.ctx.values()) {
      if (!bySource.has(c.source)) bySource.set(c.source, (await this.deps.adapter(c.source).fetchPrices?.().catch(() => ({}))) ?? {});
      const px = bySource.get(c.source)![c.symbol];
      if (px) c.price = px;
      if (!c.engine) {
        if (c.status === 'error' && !c.running) {
          c.running = true;
          await this.startLive(c)
            .catch((e) => (c.error = (e as Error).message.slice(0, 200)))
            .finally(() => (c.running = false));
        }
        continue;
      }
      if (Date.now() < c.lastBarT + 2 * M15) continue; // no new closed bar yet
      try {
        const rows = await this.deps.adapter(c.source).fetchKlines(c.symbol, '15m', 20);
        const closed = rows.filter((r) => r.t > c.lastBarT && r.t + M15 <= Date.now());
        const repo = this.deps.repo();
        if (repo && closed.length) await repo.upsertKlines(c.source, c.symbol, '15m', closed).catch(() => {});
        for (const b of closed) {
          for (const s of c.engine.step(b)) {
            c.live.push(s);
            if (c.live.length > 200) c.live.shift();
            this.deps.onLiveSetup(s);
          }
          c.lastBarT = b.t;
        }
      } catch (e) {
        c.error = `15m update: ${(e as Error).message.slice(0, 160)}`;
      }
    }
  }

  // ---------------- history (worker) ----------------

  enqueue(key: string): void {
    if (!this.queue.includes(key)) this.queue.push(key);
    void this.drain();
  }

  private async drain(): Promise<void> {
    if (this.busy) return;
    const key = this.queue.shift();
    if (!key) return;
    const c = this.ctx.get(key);
    if (!c) return void this.drain();
    this.busy = true;
    try {
      c.status = c.engine ? 'live, history running' : 'history running';
      const now = Date.now();
      const bars = await this.loadKlines(c, '15m', now - this.historyDays * DAY);
      const daily = await this.loadKlines(c, '1d', now - (this.historyDays + 400) * DAY);
      if (bars.length < 2000 || daily.length < 120) throw new Error(`not enough history for a backtest (15m ${bars.length}, D1 ${daily.length})`);
      const r = await this.analyze({ symbol: c.symbol, exchange: c.source, tick: c.tick || 0 }, daily, bars);
      c.history = r;
      const repo = this.deps.repo();
      if (repo) await repo.setSetting('levelsetups:' + key, r).catch((e) => this.deps.log(`[levels ${key}] result save failed: ${(e as Error).message}`));
      this.deps.log(`OFT_LEVELS ${JSON.stringify({ key, ms: r.ms, bars: r.coverage.bars, setups: r.setups.length, overall: r.overall, segments: r.segments, chosenBy: r.chosenBy })}`);
      c.status = c.engine ? 'live' : 'history only';
    } catch (e) {
      c.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[levels ${key}] history failed: ${c.error}`);
      c.status = c.engine ? 'live (history failed)' : 'error';
    } finally {
      this.busy = false;
      void this.drain();
    }
  }

  private analyze(meta: { symbol: string; exchange: string; tick: number }, daily: Candle[], bars: Candle[]): Promise<HistoryResult> {
    if (!this.worker) {
      this.worker = new Worker(this.deps.workerPath, { resourceLimits: { maxOldGenerationSizeMb: +(process.env.ANALYSIS_HEAP_MB ?? 160) } });
      this.worker.on('message', (m: { id: number; ok: boolean; result?: HistoryResult; error?: string }) => {
        this.pending.get(m.id)?.(m);
        this.pending.delete(m.id);
      });
      this.worker.on('error', (e) => this.deps.log(`analysis worker error: ${e.message}`));
      this.worker.on('exit', (code) => {
        this.worker = null;
        for (const [, cb] of this.pending) cb({ ok: false, error: `analysis worker exited (${code})` });
        this.pending.clear();
      });
    }
    const id = ++this.reqId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, (m) => (m.ok && m.result ? resolve(m.result) : reject(new Error(m.error ?? 'analysis failed'))));
      this.worker!.postMessage({ id, meta, daily, bars });
    });
  }

  // ---------------- views ----------------

  levels(key: string): { levels: (DailyLevel & { state: string; confirmedRecently: boolean })[]; atrD: number; status: string; error: string; price: number } | null {
    const c = this.ctx.get(key);
    if (!c) return null;
    const eng = c.engine;
    const recent = [...(c.history?.setups ?? []), ...c.live].filter((s) => s.confirmedAt > Date.now() - 3 * DAY);
    return {
      status: c.status,
      error: c.error,
      price: c.price,
      atrD: eng?.atrDaily ?? NaN,
      levels: (eng?.levels ?? []).map((l) => ({ ...l, state: eng!.stateOf(l.id), confirmedRecently: recent.some((s) => s.levelId === l.id) })),
    };
  }

  setups(key: string): { history: HistoryResult | null; live: Setup[]; status: string; error: string } | null {
    const c = this.ctx.get(key);
    if (!c) return null;
    return { history: c.history, live: c.live, status: c.status, error: c.error };
  }

  watchRow(key: string): Record<string, unknown> | null {
    const c = this.ctx.get(key);
    if (!c) return null;
    const eng = c.engine;
    const px = isFinite(c.price) ? c.price : NaN;
    const atrD = eng?.atrDaily ?? NaN;
    const act = eng ? eng.activeLevels() : [];
    const sup = act.filter((l) => l.price <= px).sort((a, b) => b.price - a.price)[0];
    const res = act.filter((l) => l.price > px).sort((a, b) => a.price - b.price)[0];
    const row = (l: DailyLevel | undefined) =>
      l ? { price: l.price, strength: l.strength, status: l.status, state: eng!.stateOf(l.id), distPct: ((l.price - px) / px) * 100, distAtr: Math.abs(l.price - px) / atrD } : null;
    return { key, source: c.source, symbol: c.symbol, price: px, atrD, support: row(sup), resistance: row(res), status: c.status, error: c.error };
  }
}
