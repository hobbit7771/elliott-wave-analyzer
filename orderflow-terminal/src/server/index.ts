// HTTP + WebSocket server: REST API, static frontend, live fan-out, persistent history (Supabase),
// server-side paper trading and alerts, retention / quota guard, diagnostics.
import http from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { existsSync, statSync, statfsSync } from 'node:fs';
import { extname, join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import { timingSafeEqual } from 'node:crypto';
import { monitorEventLoopDelay } from 'node:perf_hooks';
import { WebSocketServer } from 'ws';
import type { HeatColumn, InstrumentMeta, MarketEvent, SourceId, Trade } from '../core/types.js';
import { isTimeframe, candlesFromTrades, resampleCandles, TF_MS, type Timeframe } from '../core/candles.js';
import { columnsToCsv, downsample } from '../core/heatmap.js';
import { blockTrades, decodeBlock, replayBlock } from '../core/archiveCodec.js';
import { getAdapter, isSource, SOURCES, UNAVAILABLE_SOURCES } from './adapters/registry.js';
import { restHealth } from './adapters/adapter.js';
import { HistoryReader, openDb, symKey } from './recorder.js';
import { Hub } from './hub.js';
import { DEFAULT_DETECTOR_CONFIG, type DeepPartial, type DetectorConfig } from '../core/detectors/config.js';
import { connectDb, dbConfigFromEnv } from './persist/db.js';
import { PgRepo, type Repo } from './persist/repo.js';
import { archiveClient, archiveConfigFromEnv, type ArchiveClient } from './persist/archive.js';
import { PaperService, type PaperState, type BookView } from './services/paper.js';
import { AlertService, DEFAULT_RULES, type AlertRule } from './services/alerts.js';
import { RetentionService, policyFromEnv } from './services/retention.js';

const here = dirname(fileURLToPath(import.meta.url));
const PORT = +(process.env.PORT ?? 8080);
const HOST = process.env.HOST ?? '0.0.0.0';
const DB_PATH = process.env.DB_PATH ?? resolve(process.cwd(), 'data/orderflow.sqlite');
const WEB_DIR = process.env.WEB_DIR ?? resolve(here, '../web');
const OWNER_TOKEN = process.env.OWNER_TOKEN ?? '';
const PROD = process.env.NODE_ENV === 'production';
const log = (m: string) => console.log(`${new Date().toISOString()} ${m}`);

// Guard: in production market data may only come from the official endpoints of connected sources.
if (PROD) {
  const allowed: Record<string, RegExp> = {
    BINANCE_FUTURES_REST: /(^|\.)binance\.com$/,
    BINANCE_FUTURES_WS_BASE: /(^|\.)binance\.com$/,
    BINANCE_SPOT_REST: /(^|\.)binance\.(com|vision)$/,
    BINANCE_SPOT_WS: /(^|\.)binance\.(com|vision)$/,
    DATABENTO_LIVE_GATEWAY: /(^|\.)databento\.com$/,
    OFT_ARCHIVE_URL: /(^|\.)supabase\.co$/,
    OFT_DB_URL: /(^|\.)supabase\.(co|com)$/,
    DATABENTO_HIST_URL: /(^|\.)databento\.com$/,
  };
  for (const [k, re] of Object.entries(allowed)) {
    const v = process.env[k];
    if (!v) continue;
    let host = '';
    try {
      host = new URL(v.includes('://') ? v : `tcp://${v}`).hostname;
    } catch {
      /* invalid */
    }
    if (!re.test(host)) {
      console.error(`${k}=${v} rejected: production data must come from the official endpoints of the source`);
      process.exit(1);
    }
  }
}

const db = openDb(DB_PATH);
const reader = new HistoryReader(db);

// ---------------- Supabase (optional but required for persistent history) ----------------
let repo: Repo | null = null;
let archive: ArchiveClient | null = null;
let supabaseState = { configured: false, connected: false, host: '', error: '' };
const dbCfg = dbConfigFromEnv();
const archCfg = archiveConfigFromEnv();
if (archCfg) archive = archiveClient(archCfg);

// ---------------- services ----------------
let paper: PaperService | null = null;
let alerts: AlertService = new AlertService(DEFAULT_RULES, async () => true, (a) => hub.broadcastAll('alert', a));
let retention: RetentionService | null = null;
const lastDeriv = new Map<string, { funding?: number; mark?: number; nextFunding?: number }>();
const lastPersistError = new Map<string, string>();

const hub: Hub = new Hub({
  workerPath: resolve(here, 'worker.js'),
  dbPath: DB_PATH,
  maxSessions: +(process.env.MAX_SESSIONS ?? 3),
  idleStopMs: +(process.env.IDLE_STOP_MS ?? 15 * 60_000),
  pinned: [],
  backfillMinutes: +(process.env.BACKFILL_MINUTES ?? 10),
  reader,
  log,
  tapChannels: new Set(['book', 'event', 'status', 'deriv']),
  tap: (key, ch, d) => {
    if (ch === 'book') {
      const b = d as { t: number; bids: [number, number][]; asks: [number, number][] };
      const st = hub.lastStatus(key);
      const meta = hub.sessions.get(key)?.meta;
      paper?.onBook(key, { t: Date.now(), bids: b.bids, asks: b.asks, gate: !!st?.gate, tick: meta?.tickSize ?? 0 } satisfies BookView);
    } else if (ch === 'event') alerts.onEvent(d as MarketEvent);
    else if (ch === 'deriv') {
      const x = d as { funding?: number; mark?: number; nextFunding?: number };
      lastDeriv.set(key, x);
      if (x.funding !== undefined && x.mark && x.nextFunding) paper?.onFunding(key, x.funding, x.mark, x.nextFunding);
    } else if (ch === 'status') {
      // history-write failures are alertable feed events
      const p = (d as { persist?: { lastError?: string } }).persist;
      const err = p?.lastError ?? '';
      if (err && err !== lastPersistError.get(key)) {
        const [source, symbol] = key.split(':');
        alerts.onEvent({ id: `persist-${key}-${Date.now()}`, t: Date.now(), kind: 'feed', title: 'Ошибка записи истории', price: NaN, confidence: 100, explain: err, source: source as SourceId, symbol });
      }
      lastPersistError.set(key, err);
    }
  },
});

async function initSupabase(attempt = 0): Promise<void> {
  if (!dbCfg) {
    supabaseState = { configured: false, connected: false, host: '', error: 'SUPABASE_PROJECT_REF / OFT_DB_PASSWORD not set: history is NOT persisted' };
    log(supabaseState.error);
    return;
  }
  supabaseState.configured = true;
  try {
    const { sql, host } = await connectDb(dbCfg, log, 3);
    repo = new PgRepo(sql);
    const r0 = repo;
    hub.metaStore = { get: (src, sym) => r0.getInstrument<InstrumentMeta>(src, sym), put: (m) => r0.putInstrument(m) };
    supabaseState = { configured: true, connected: true, host, error: '' };
    // restore settings: paper state, alert rules, detector configs, record list
    const r = repo;
    paper = new PaperService((await r.getPaper('default')) as PaperState | undefined, (s) => r.setPaper('default', s), log);
    const rules = (await r.getSetting<AlertRule[]>('alert_rules')) ?? DEFAULT_RULES;
    alerts = new AlertService(
      rules,
      (a) => r.logAlert({ t: a.t, ruleId: a.ruleId, source: a.event.source, symbol: a.event.symbol, eventId: a.event.id, body: a }),
      (a) => hub.broadcastAll('alert', a),
    );
    for (const s of (await r.getSetting<[string, unknown][]>('detector_configs')) ?? []) reader.setSetting('cfg:' + s[0], s[1]);
    retention = new RetentionService(r, archive, policyFromEnv(), log, (p) => hub.toWorkers({ op: 'policy', policy: p }));
    setTimeout(() => void retention?.run(), 60_000);
    setInterval(() => void retention?.run(), 10 * 60_000);
    setInterval(() => {
      for (const k of hub.pinned) {
        const [src, sym] = k.split(':');
        void retention?.verifyArchive(src, sym, src === 'binance-futures');
      }
    }, 15 * 60_000);
    let list = await r.recordList();
    if (!list.length) {
      for (const k of (process.env.DEFAULT_SYMBOLS ?? 'binance-futures:BTCUSDT,binance-futures:ETHUSDT').split(',').map((s) => s.trim()).filter(Boolean)) {
        const [src, sym] = k.split(':');
        await r.setRecord(src, sym, true);
      }
      list = await r.recordList();
    }
    startRecording(list.filter((x) => x.enabled).map((x) => symKey(x.source, x.symbol)));
  } catch (e) {
    supabaseState = { configured: true, connected: false, host: '', error: (e as Error).message };
    const wait = Math.min(300_000, 10_000 * 2 ** attempt);
    log(`Supabase unavailable (${supabaseState.error}); live data continues WITHOUT persistent history; retry in ${wait / 1000}s`);
    if (!hub.pinned.length) startRecording((process.env.DEFAULT_SYMBOLS ?? 'binance-futures:BTCUSDT').split(',').map((s) => s.trim()).filter(Boolean));
    setTimeout(() => void initSupabase(attempt + 1), wait);
  }
}

function startRecording(keys: string[]): void {
  hub.setPinned(keys);
  for (const k of keys) {
    const [src, sym] = k.split(':');
    if (!isSource(src) || !sym) continue;
    const tryStart = (attempt: number): void => {
      hub.ensure(src, sym).catch((e) => {
        const delay = Math.min(60_000, 5000 * 2 ** attempt);
        log(`[${k}] could not start recording session (retry in ${delay / 1000}s): ${e.message}`);
        setTimeout(() => tryStart(attempt + 1), delay);
      });
    };
    tryStart(0);
  }
}

const loop = monitorEventLoopDelay({ resolution: 20 });
const lag = (ns: number): number => +Math.max(0, ns / 1e6 - 20).toFixed(2);
loop.enable();

const klineCache = new Map<string, { t: number; data: unknown }>();

function json(res: http.ServerResponse, code: number, body: unknown, extra: Record<string, string> = {}): void {
  const s = JSON.stringify(body);
  const gz = s.length > 2048 && /\bgzip\b/.test(String(res.req?.headers['accept-encoding'] ?? ''));
  const buf = gz ? gzipSync(s) : Buffer.from(s);
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...(gz ? { 'content-encoding': 'gzip', vary: 'accept-encoding' } : {}), ...extra });
  res.end(buf);
}

function sendText(res: http.ServerResponse, body: string, type: string, filename?: string): void {
  res.writeHead(200, { 'content-type': type, ...(filename ? { 'content-disposition': `attachment; filename="${filename}"` } : {}) });
  res.end(body);
}

const httpErr = (status: number, msg: string) => Object.assign(new Error(msg), { status });

function params(u: URL): { source: SourceId; symbol: string; key: string } {
  const source = u.searchParams.get('source') ?? 'binance-futures';
  const symbol = (u.searchParams.get('symbol') ?? '').toUpperCase();
  if (!isSource(source)) throw httpErr(400, 'bad source');
  if (!/^[A-Z0-9]{2,30}$/.test(symbol)) throw httpErr(400, 'bad symbol');
  return { source, symbol, key: symKey(source, symbol) };
}

const num = (u: URL, k: string, d: number): number => {
  const v = u.searchParams.get(k);
  const n = v === null ? NaN : Number(v);
  return isFinite(n) ? n : d;
};

async function readBody(req: http.IncomingMessage): Promise<unknown> {
  let s = '';
  for await (const ch of req) {
    s += ch;
    if (s.length > 200_000) throw httpErr(413, 'body too large');
  }
  try {
    return s ? JSON.parse(s) : {};
  } catch {
    throw httpErr(400, 'invalid JSON');
  }
}

/** Owner authorization for every action that changes state or reads private state. */
function requireOwner(req: http.IncomingMessage): void {
  if (!OWNER_TOKEN) throw httpErr(403, 'OWNER_TOKEN is not configured on the server: owner actions are disabled');
  const got = Buffer.from(String(req.headers['x-oft-owner'] ?? ''));
  const want = Buffer.from(OWNER_TOKEN);
  if (got.length !== want.length || !timingSafeEqual(got, want)) throw httpErr(401, 'owner token required');
}

function needRepo(): Repo {
  if (!repo) throw httpErr(503, `persistent history unavailable: ${supabaseState.error || 'connecting'}`);
  return repo;
}

/** heat columns: local 1 s cache for the recent part, Supabase 10 s / 60 s tiles for older ranges */
async function heatHistory(source: string, symbol: string, key: string, from: number, to: number, maxCols: number): Promise<{ cols: HeatColumn[]; res: number; tiers: string[] }> {
  const local = reader.heat(key, from, to, maxCols);
  const localFrom = local.cols.length ? local.cols[0].t : Infinity;
  const tiers = local.cols.length ? [`local:${local.res}s`] : [];
  let cols = local.cols;
  if (repo && from < localFrom - 15_000) {
    const span = Math.min(to, localFrom) - from;
    const res = span / 10_000 <= maxCols * 2 ? 10 : 60;
    let older = await repo.heat(source, symbol, res, from, Math.min(to, localFrom - 1), 20_000);
    if (!older.length && res === 10) older = await repo.heat(source, symbol, 60, from, Math.min(to, localFrom - 1), 20_000);
    if (older.length) {
      tiers.push(`supabase:${older[0].dt >= 30_000 ? 60 : 10}s`);
      cols = older.concat(cols);
    }
  }
  if (cols.length > maxCols) cols = downsample(cols, Math.max(1000, Math.ceil((to - from) / maxCols / 1000) * 1000));
  return { cols, res: local.res, tiers };
}

async function eventsHistory(source: string, symbol: string, key: string, from: number, to: number, limit: number, asOf?: number): Promise<MarketEvent[]> {
  if (asOf !== undefined) return needRepo().events(source, symbol, from, to, limit, asOf);
  const m = new Map<string, MarketEvent>();
  if (repo) for (const e of await repo.events(source, symbol, from, to, limit)) m.set(e.id, e);
  for (const e of reader.events(key, from, to, limit)) m.set(e.id, e);
  return [...m.values()].sort((a, b) => a.t - b.t).slice(-limit);
}

// Archive blocks are decoded in the main thread one at a time (a decoded block is tens of MB), and only the
// extracted trades are kept (small LRU): concurrent history requests after a restart must not exhaust the heap.
const blockTradeCache = new Map<string, Trade[]>();
let decodeChain: Promise<unknown> = Promise.resolve();
function serialDecode<T>(fn: () => Promise<T>): Promise<T> {
  const run = decodeChain.then(fn, fn);
  decodeChain = run.catch(() => {});
  return run;
}
async function archivedTrades(m: { path: string; sha256: string }): Promise<Trade[]> {
  const hit = blockTradeCache.get(m.path);
  if (hit) {
    blockTradeCache.delete(m.path);
    blockTradeCache.set(m.path, hit);
    return hit;
  }
  const trades = await serialDecode(async () => blockTrades(decodeBlock(await archive!.get(m.path), m.sha256)));
  blockTradeCache.set(m.path, trades);
  if (blockTradeCache.size > 24) blockTradeCache.delete(blockTradeCache.keys().next().value!);
  return trades;
}

/** trades: local cache, plus WebSocket trades from Supabase archive blocks for older ranges (bounded) */
async function tradesHistory(source: string, symbol: string, key: string, from: number, to: number, limit: number): Promise<{ trades: Trade[]; sources: string[] }> {
  const local = reader.trades(key, from, to, limit);
  const localFrom = local.length ? local[0].t : Infinity;
  const sources = local.length ? ['local'] : [];
  if (!repo || !archive || from >= localFrom - 5000) return { trades: local, sources };
  const ms = (await repo.manifests(source, symbol, from, Math.min(to, localFrom))).slice(-12);
  const extra: Trade[] = [];
  for (const m of ms) {
    try {
      for (const t of await archivedTrades(m)) if (t.t >= from && t.t < localFrom) extra.push(t);
    } catch (e) {
      log(`archive read ${m.path} failed: ${(e as Error).message}`);
    }
  }
  if (extra.length) sources.push(`archive:${ms.length} blocks`);
  const byId = new Map<string, Trade>();
  for (const t of extra.concat(local)) byId.set(t.id !== undefined ? 'i' + t.id : `${t.t}|${t.price}|${t.qty}`, t);
  return { trades: [...byId.values()].sort((a, b) => a.t - b.t || (a.id ?? 0) - (b.id ?? 0)).slice(-limit), sources };
}

async function api(req: http.IncomingMessage, res: http.ServerResponse, u: URL): Promise<void> {
  const p = u.pathname;
  const m = req.method ?? 'GET';
  if (p === '/api/health') return json(res, 200, { ok: true, t: Date.now(), sessions: hub.sessions.size, supabase: supabaseState.connected });
  if (p === '/api/sources') {
    return json(res, 200, {
      available: SOURCES.map((id) => {
        const a = getAdapter(id);
        return { id, name: a.name, caps: a.caps, limitations: a.limitations, status: 'implemented' };
      }),
      unavailable: UNAVAILABLE_SOURCES,
    });
  }
  if (p === '/api/instruments') {
    const source = u.searchParams.get('source') ?? 'binance-futures';
    if (!isSource(source)) return json(res, 400, { error: 'bad source' });
    try {
      return json(res, 200, await getAdapter(source).listInstruments(), { 'cache-control': 'max-age=600' });
    } catch (e) {
      // exchange REST unavailable: the instruments recorded before are still usable
      const stored = repo ? await repo.listInstruments<InstrumentMeta>(source).catch(() => []) : [];
      if (!stored.length) throw e;
      return json(res, 200, stored, { 'x-oft-origin': 'stored' });
    }
  }
  if (p === '/api/klines') {
    const { source, symbol, key } = params(u);
    const tf = u.searchParams.get('tf') ?? '1m';
    if (!isTimeframe(tf)) return json(res, 400, { error: 'bad timeframe' });
    const limit = Math.min(1500, Math.max(10, num(u, 'limit', 1000)));
    const end = u.searchParams.get('end') ? num(u, 'end', Date.now()) : undefined;
    const ad = getAdapter(source);
    if (ad.caps.nativeIntervals.includes(tf)) {
      const ck = `${key}:${tf}:${limit}:${end ?? 'now'}`;
      const c = klineCache.get(ck);
      if (c && Date.now() - c.t < (end ? 600_000 : 2000)) return json(res, 200, c.data);
      // the exchange includes the forming bar: flag it so the client never treats it as closed
      const barMs = TF_MS[tf as keyof typeof TF_MS];
      const liveFrom = Math.floor(Date.now() / barMs) * barMs;
      let data: Record<string, unknown>;
      try {
        data = { candles: await ad.fetchKlines(symbol, tf, limit, end), origin: 'exchange-rest', tf, liveFrom };
      } catch (e) {
        // exchange REST unavailable (e.g. shared cloud IP banned): fall back to stored closed 1m candles
        if (!repo) throw e;
        const to = end ?? Date.now();
        const stored = await repo.candles(source, symbol, to - Math.min(limit * barMs, 14 * 86_400_000), to);
        if (!stored.length) throw e;
        const candles = barMs === 60_000 ? stored : resampleCandles(stored, tf as Exclude<Timeframe, 'tick'>);
        data = { candles: candles.slice(-limit), origin: 'stored-1m', tf, liveFrom, warning: `Exchange REST unavailable (${(e as Error).message.slice(0, 120)}); showing closed 1m candles stored in Supabase, resampled` };
        return json(res, 200, data);
      }
      klineCache.set(ck, { t: Date.now(), data });
      if (klineCache.size > 300) klineCache.delete(klineCache.keys().next().value!);
      return json(res, 200, data);
    }
    // tick and non-native 1s: built from recorded (live + backfilled) aggTrades
    const to = end ?? Date.now();
    const ticksPerBar = Math.max(10, Math.min(5000, num(u, 'ticks', 100)));
    const span = tf === 'tick' ? 6 * 3600_000 : Math.min(6 * 3600_000, limit * TF_MS['1s']);
    const trades = reader.trades(key, to - span, to, tf === 'tick' ? limit * ticksPerBar : 400_000);
    const candles = candlesFromTrades(trades, tf, ticksPerBar).slice(-limit);
    return json(res, 200, { candles, origin: 'recorded-trades', tf, coverage: reader.tradeRange(key), note: 'aggTrade messages: one message can aggregate several fills at the same price' });
  }
  if (p === '/api/trades') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 3600_000);
    const limit = Math.min(300_000, num(u, 'limit', 100_000));
    const r = await tradesHistory(source, symbol, key, from, to, limit);
    return json(res, 200, { trades: r.trades.map((t) => [t.t, t.price, t.qty, t.side, t.id ?? 0]), coverage: reader.tradeRange(key), sources: r.sources });
  }
  if (p === '/api/heatmap') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 15 * 60_000);
    const maxCols = Math.min(4000, Math.max(50, num(u, 'maxCols', 1500)));
    const r = await heatHistory(source, symbol, key, from, to, maxCols);
    const gaps = repo ? await repo.gaps(source, symbol, from, to).catch(() => []) : [];
    return json(res, 200, { ...r, coverage: reader.heatRange(key), gaps });
  }
  if (p === '/api/events') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 24 * 3600_000);
    const asOf = u.searchParams.get('asOf') ? num(u, 'asOf', to) : undefined;
    return json(res, 200, await eventsHistory(source, symbol, key, from, to, Math.min(10_000, num(u, 'limit', 3000)), asOf));
  }
  if (p === '/api/candles1m') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 24 * 3600_000);
    return json(res, 200, repo ? await repo.candles(source, symbol, from, to) : reader.candles1m(key, from, to));
  }
  if (p === '/api/gaps') {
    const { source, symbol } = params(u);
    const to = num(u, 'to', Date.now());
    const r = needRepo();
    return json(res, 200, { gaps: await r.gaps(source, symbol, num(u, 'from', to - 7 * 86_400_000), to), coverage: await r.coverage(source, symbol) });
  }
  if (p === '/api/archive/book') {
    // exact L2 state (inside the archived band) at time t, reconstructed from snapshot + continuous diffs
    const { source, symbol } = params(u);
    const t = num(u, 't', Date.now());
    const r = needRepo();
    if (!archive) throw httpErr(503, 'archive storage not configured');
    const ms = await r.manifests(source, symbol, t, t);
    if (!ms.length) return json(res, 404, { error: 'no archived L2 block covers this time (outside retention or a recording gap)' });
    const a = archive;
    const { blk, rep } = await serialDecode(async () => {
      const blk = decodeBlock(await a.get(ms[0].path), ms[0].sha256);
      return { blk: { loTick: blk.loTick, hiTick: blk.hiTick, tick: blk.tick }, rep: replayBlock(blk, t, source === 'binance-futures') };
    });
    const top = rep.book.top(num(u, 'levels', 50));
    return json(res, 200, { t, block: ms[0].path, continuous: rep.continuous, lastUpdateId: rep.lastUpdateId, band: [blk.loTick * blk.tick, blk.hiTick * blk.tick], ...top });
  }
  if (p === '/api/export/heatmap.csv') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 15 * 60_000);
    const { cols } = await heatHistory(source, symbol, key, from, to, 3000);
    return sendText(res, columnsToCsv(cols), 'text/csv', `heatmap_${symbol}_${from}_${to}.csv`);
  }
  if (p === '/api/export/trades.csv') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 3600_000);
    const r = await tradesHistory(source, symbol, key, from, to, 300_000);
    const rows = r.trades.map((t) => `${new Date(t.t).toISOString()},${t.id ?? ''},${t.price},${t.qty},${t.side === 1 ? 'buy' : 'sell'}`);
    return sendText(res, 'time_utc,agg_trade_id,price,qty,aggressor\n' + rows.join('\n'), 'text/csv', `trades_${symbol}.csv`);
  }
  if (p === '/api/export/events.json') {
    const { source, symbol, key } = params(u);
    const to = num(u, 'to', Date.now());
    return sendText(res, JSON.stringify(await eventsHistory(source, symbol, key, num(u, 'from', to - 24 * 3600_000), to, 10_000), null, 1), 'application/json', `events_${symbol}.json`);
  }
  if (p === '/api/config') {
    const { key } = params(u);
    if (m === 'GET') return json(res, 200, { config: hub.config(key), defaults: DEFAULT_DETECTOR_CONFIG });
    requireOwner(req);
    const cfg = hub.setConfig(key, (await readBody(req)) as DeepPartial<DetectorConfig>);
    if (repo) {
      const all = ((await repo.getSetting<[string, unknown][]>('detector_configs')) ?? []).filter((x) => x[0] !== key);
      all.push([key, cfg]);
      await repo.setSetting('detector_configs', all);
    }
    return json(res, 200, { config: cfg });
  }
  if (p === '/api/auth/check') {
    requireOwner(req);
    return json(res, 200, { ok: true });
  }
  if (p === '/api/record-list') {
    const r = needRepo();
    if (m === 'GET') return json(res, 200, { list: await r.recordList(), recording: hub.pinned, maxSessions: +(process.env.MAX_SESSIONS ?? 3) });
    requireOwner(req);
    const b = (await readBody(req)) as { source?: string; symbol?: string; enabled?: boolean };
    if (!b.source || !isSource(b.source) || !/^[A-Z0-9]{2,30}$/.test(String(b.symbol))) throw httpErr(400, 'bad instrument');
    const list = await r.recordList();
    if (b.enabled && list.filter((x) => x.enabled).length >= +(process.env.MAX_SESSIONS ?? 3)) throw httpErr(409, 'recording limit reached for this plan (MAX_SESSIONS)');
    await r.setRecord(b.source, String(b.symbol), !!b.enabled);
    const now = (await r.recordList()).filter((x) => x.enabled).map((x) => symKey(x.source, x.symbol));
    for (const k of hub.pinned) if (!now.includes(k)) hub.stop(k);
    startRecording(now);
    return json(res, 200, { list: await r.recordList() });
  }
  if (p === '/api/objects') {
    // user drawings / levels per instrument
    const { key } = params(u);
    const r = needRepo();
    if (m === 'GET') return json(res, 200, (await r.getSetting('objects:' + key)) ?? []);
    requireOwner(req);
    const body = await readBody(req);
    if (!Array.isArray(body) || JSON.stringify(body).length > 100_000) throw httpErr(400, 'objects must be an array (<100 KB)');
    await r.setSetting('objects:' + key, body);
    return json(res, 200, { ok: true });
  }
  if (p.startsWith('/api/paper')) {
    requireOwner(req);
    if (!paper) throw httpErr(503, 'paper trading needs persistent storage (Supabase) which is not connected');
    if (p === '/api/paper' && m === 'GET') return json(res, 200, paper.view());
    const b = (await readBody(req)) as { source?: string; symbol?: string; side?: number; qty?: number; sl?: number; tp?: number; id?: number; takerFee?: number; extraSlippageTicks?: number };
    if (p === '/api/paper/order') {
      if (!b.source || !isSource(b.source) || !b.symbol) throw httpErr(400, 'bad instrument');
      if (b.side !== 1 && b.side !== -1) throw httpErr(400, 'side must be 1 or -1');
      try {
        return json(res, 200, paper.order(symKey(b.source, b.symbol), b.side, Number(b.qty), b.sl, b.tp));
      } catch (e) {
        throw httpErr(409, (e as Error).message);
      }
    }
    if (p === '/api/paper/close') {
      try {
        return json(res, 200, paper.close(Number(b.id)));
      } catch (e) {
        throw httpErr(409, (e as Error).message);
      }
    }
    if (p === '/api/paper/config') {
      paper.setConfig({ takerFee: b.takerFee, extraSlippageTicks: b.extraSlippageTicks });
      return json(res, 200, paper.view());
    }
    if (p === '/api/paper/reset') {
      paper.reset();
      return json(res, 200, paper.view());
    }
  }
  if (p === '/api/alerts') {
    requireOwner(req);
    const r = needRepo();
    if (m === 'GET') return json(res, 200, { rules: alerts.rules, log: await r.alertLog(200) });
    const rules = (await readBody(req)) as AlertRule[];
    if (!Array.isArray(rules)) throw httpErr(400, 'rules must be an array');
    alerts.rules = rules;
    await r.setSetting('alert_rules', rules);
    return json(res, 200, { rules });
  }
  if (p === '/api/history' && m === 'DELETE') {
    requireOwner(req);
    const { source, symbol, key } = params(u);
    const from = num(u, 'from', NaN);
    const to = num(u, 'to', NaN);
    if (!isFinite(from) || !isFinite(to) || to <= from) throw httpErr(400, 'explicit from/to range required');
    const out = repo ? await repo.deleteRange(source, symbol, from, to) : {};
    reader.clear(key);
    return json(res, 200, { ok: true, deleted: out });
  }
  if (p === '/api/diag' || p === '/api/perf') {
    const mem = process.memoryUsage();
    return json(res, 200, {
      memory: memReport(),
      at: new Date().toISOString(),
      uptimeSec: Math.round(process.uptime()),
      node: process.version,
      memoryMB: { rss: +(mem.rss / 1048576).toFixed(1), heapUsed: +(mem.heapUsed / 1048576).toFixed(1), external: +(mem.external / 1048576).toFixed(1) },
      cpu: process.cpuUsage(),
      eventLoopLagMs: { p50: lag(loop.percentile(50)), p99: lag(loop.percentile(99)), max: lag(loop.max) },
      hub: hub.info(),
      localCache: reader.sizeInfo(),
      supabase: supabaseState,
      exchangeRest: restHealth,
      storage: retention?.status ?? null,
      recording: hub.pinned,
      ownerActions: OWNER_TOKEN ? 'enabled (token required)' : 'disabled (OWNER_TOKEN not set)',
    });
  }
  return json(res, 404, { error: 'not found' });
}

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.webmanifest': 'application/manifest+json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain',
};
const gzCache = new Map<string, Buffer>();

async function serveStatic(req: http.IncomingMessage, res: http.ServerResponse, u: URL): Promise<void> {
  let rel = decodeURIComponent(u.pathname);
  if (rel.includes('..')) {
    res.writeHead(400).end();
    return;
  }
  if (rel === '/') rel = '/index.html';
  let file = join(WEB_DIR, rel);
  if (!existsSync(file) || !(await stat(file)).isFile()) file = join(WEB_DIR, 'index.html');
  if (!existsSync(file)) {
    res.writeHead(503, { 'content-type': 'text/plain' }).end('frontend not built: run `npm run build`');
    return;
  }
  const ext = extname(file);
  const type = MIME[ext] ?? 'application/octet-stream';
  const immutable = rel.startsWith('/assets/');
  const headers: Record<string, string> = { 'content-type': type, 'cache-control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache' };
  const accepts = /\bgzip\b/.test(String(req.headers['accept-encoding'] ?? ''));
  if (accepts && /text|javascript|json|svg|manifest/.test(type)) {
    let gz = gzCache.get(file);
    if (!gz) {
      gz = gzipSync(await readFile(file));
      if (immutable) gzCache.set(file, gz);
    }
    res.writeHead(200, { ...headers, 'content-encoding': 'gzip', vary: 'accept-encoding' });
    res.end(gz);
    return;
  }
  res.writeHead(200, headers);
  res.end(await readFile(file));
}

// memory evidence for the 512 MB free instance: process RSS, main heap, each worker heap, local cache
// file size, and whether the cache directory is RAM-backed (tmpfs files count against container memory)
function memReport(): Record<string, unknown> {
  const m = process.memoryUsage();
  let cacheBytes = 0;
  for (const f of [DB_PATH, DB_PATH + '-wal']) {
    try {
      cacheBytes += statSync(f).size;
    } catch {
      /* not there */
    }
  }
  let tmpfs: boolean | null = null;
  try {
    tmpfs = Number(statfsSync(dirname(DB_PATH)).type) === 0x01021994;
  } catch {
    tmpfs = null;
  }
  const mb = (b: number) => Math.round(b / 1048576);
  return {
    rssMb: mb(m.rss),
    mainHeapMb: mb(m.heapUsed),
    externalMb: mb(m.external),
    workersHeapMb: Object.fromEntries([...hub.sessions.values()].map((l) => [l.key, l.heapMb ?? null])),
    cacheMb: mb(cacheBytes),
    cacheOnTmpfs: tmpfs,
    cachePath: DB_PATH,
  };
}
setInterval(() => log('OFT_MEM ' + JSON.stringify(memReport())), 60_000).unref();

const server = http.createServer((req, res) => {
  const u = new URL(req.url ?? '/', 'http://localhost');
  const h = u.pathname.startsWith('/api/') ? api(req, res, u) : serveStatic(req, res, u);
  h.catch((e: Error & { status?: number }) => {
    const code = e.status ?? (/\b451\b/.test(e.message) ? 451 : 502);
    if (!res.headersSent) json(res, code, { error: e.message });
    else res.end();
  });
});

const wss = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024, perMessageDeflate: { threshold: 1024, zlibDeflateOptions: { level: 3 } } });
server.on('upgrade', (req, socket, head) => {
  if (!req.url?.startsWith('/ws')) {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    hub.addClient(ws);
    const iv = setInterval(() => ws.readyState === 1 && ws.ping(), 25_000);
    ws.on('close', () => clearInterval(iv));
  });
});

server.listen(PORT, HOST, () => {
  log(`OrderFlow Terminal listening on ${HOST}:${PORT} (local cache ${DB_PATH}, node ${process.version})`);
  void initSupabase();
});

let shuttingDown = false;
const shutdown = async (sig: string) => {
  if (shuttingDown) return;
  shuttingDown = true;
  log(`${sig}: flushing history queues and shutting down`);
  server.close();
  await hub.shutdown(15_000);
  try {
    db.close();
  } catch {
    /* already closed */
  }
  log('shutdown complete');
  process.exit(0);
};
process.on('SIGTERM', () => void shutdown('SIGTERM'));
process.on('SIGINT', () => void shutdown('SIGINT'));
