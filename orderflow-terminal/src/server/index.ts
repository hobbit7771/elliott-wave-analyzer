// HTTP + WebSocket server: REST API, static frontend, live fan-out.
import http from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { extname, join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import { monitorEventLoopDelay } from 'node:perf_hooks';
import { WebSocketServer } from 'ws';
import type { SourceId } from '../core/types.js';
import { isTimeframe, candlesFromTrades, TF_MS } from '../core/candles.js';
import { columnsToCsv } from '../core/heatmap.js';
import { getAdapter, isSource, SOURCES, UNAVAILABLE_SOURCES } from './adapters/registry.js';
import { HistoryReader, openDb, symKey } from './recorder.js';
import { Hub } from './hub.js';
import { DEFAULT_DETECTOR_CONFIG } from '../core/detectors/config.js';

const here = dirname(fileURLToPath(import.meta.url));
const PORT = +(process.env.PORT ?? 8080);
const DB_PATH = process.env.DB_PATH ?? resolve(process.cwd(), 'data/orderflow.sqlite');
const WEB_DIR = process.env.WEB_DIR ?? resolve(here, '../web');
const log = (m: string) => console.log(`${new Date().toISOString()} ${m}`);

// Guard: in production, market data may only come from the real exchange endpoints.
// (Endpoint overrides exist for exchange URL changes; tests point them at a local test venue.)
if (process.env.NODE_ENV === 'production') {
  for (const k of ['BINANCE_FUTURES_REST', 'BINANCE_FUTURES_WS', 'BINANCE_SPOT_REST', 'BINANCE_SPOT_WS']) {
    const v = process.env[k];
    if (!v) continue;
    let host = '';
    try {
      host = new URL(v).hostname;
    } catch {
      /* invalid */
    }
    if (!/(^|\.)binance\.(com|vision)$/.test(host) || !/^(https|wss):/.test(v)) {
      console.error(`${k}=${v} rejected: production data must come from official Binance endpoints`);
      process.exit(1);
    }
  }
}

const db = openDb(DB_PATH);
const reader = new HistoryReader(db);
const hub = new Hub({
  workerPath: resolve(here, 'worker.js'),
  dbPath: DB_PATH,
  maxSessions: +(process.env.MAX_SESSIONS ?? 3),
  idleStopMs: +(process.env.IDLE_STOP_MS ?? 15 * 60_000),
  pinned: (process.env.DEFAULT_SYMBOLS ?? 'binance-futures:BTCUSDT').split(',').map((s) => s.trim()).filter(Boolean),
  backfillMinutes: +(process.env.BACKFILL_MINUTES ?? 10),
  reader,
  log,
});

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

function params(u: URL): { source: SourceId; symbol: string; key: string } {
  const source = u.searchParams.get('source') ?? 'binance-futures';
  const symbol = (u.searchParams.get('symbol') ?? '').toUpperCase();
  if (!isSource(source)) throw Object.assign(new Error('bad source'), { status: 400 });
  if (!/^[A-Z0-9]{2,30}$/.test(symbol)) throw Object.assign(new Error('bad symbol'), { status: 400 });
  return { source, symbol, key: symKey(source, symbol) };
}

const num = (u: URL, k: string, d: number): number => {
  const v = u.searchParams.get(k);
  const n = v === null ? NaN : Number(v);
  return isFinite(n) ? n : d;
};

async function readBody(req: http.IncomingMessage): Promise<string> {
  let s = '';
  for await (const ch of req) {
    s += ch;
    if (s.length > 100_000) throw Object.assign(new Error('body too large'), { status: 413 });
  }
  return s;
}

async function api(req: http.IncomingMessage, res: http.ServerResponse, u: URL): Promise<void> {
  const p = u.pathname;
  if (p === '/api/health') return json(res, 200, { ok: true, t: Date.now(), sessions: hub.sessions.size });
  if (p === '/api/sources') {
    return json(res, 200, {
      available: SOURCES.map((id) => {
        const a = getAdapter(id);
        return { id, name: a.name, caps: a.caps, limitations: a.limitations };
      }),
      unavailable: UNAVAILABLE_SOURCES,
    });
  }
  if (p === '/api/instruments') {
    const source = u.searchParams.get('source') ?? 'binance-futures';
    if (!isSource(source)) return json(res, 400, { error: 'bad source' });
    return json(res, 200, await getAdapter(source).listInstruments(), { 'cache-control': 'max-age=600' });
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
      const candles = await ad.fetchKlines(symbol, tf, limit, end);
      const data = { candles, origin: 'exchange-rest', tf };
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
    return json(res, 200, { candles, origin: 'recorded-trades', tf, coverage: reader.tradeRange(key) });
  }
  if (p === '/api/trades') {
    const { key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 3600_000);
    const limit = Math.min(300_000, num(u, 'limit', 100_000));
    const trades = reader.trades(key, from, to, limit);
    // compact tuples [t, price, qty, side, aggTradeId]
    return json(res, 200, { trades: trades.map((t) => [t.t, t.price, t.qty, t.side, t.id ?? 0]), coverage: reader.tradeRange(key) });
  }
  if (p === '/api/heatmap') {
    const { key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 15 * 60_000);
    const maxCols = Math.min(4000, Math.max(50, num(u, 'maxCols', 1500)));
    const r = reader.heat(key, from, to, maxCols);
    return json(res, 200, { ...r, coverage: reader.heatRange(key) });
  }
  if (p === '/api/events') {
    const { key } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 24 * 3600_000);
    return json(res, 200, reader.events(key, from, to, Math.min(10_000, num(u, 'limit', 3000))));
  }
  if (p === '/api/candles1m') {
    const { key } = params(u);
    const to = num(u, 'to', Date.now());
    return json(res, 200, reader.candles1m(key, num(u, 'from', to - 24 * 3600_000), to));
  }
  if (p === '/api/export/heatmap.csv') {
    const { key, symbol } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 15 * 60_000);
    const { cols } = reader.heat(key, from, to, 3000);
    return sendText(res, columnsToCsv(cols), 'text/csv', `heatmap_${symbol}_${from}_${to}.csv`);
  }
  if (p === '/api/export/trades.csv') {
    const { key, symbol } = params(u);
    const to = num(u, 'to', Date.now());
    const from = num(u, 'from', to - 3600_000);
    const rows = reader.trades(key, from, to, 300_000).map((t) => `${new Date(t.t).toISOString()},${t.price},${t.qty},${t.side === 1 ? 'buy' : 'sell'}`);
    return sendText(res, 'time_utc,price,qty,aggressor\n' + rows.join('\n'), 'text/csv', `trades_${symbol}.csv`);
  }
  if (p === '/api/export/events.json') {
    const { key, symbol } = params(u);
    const to = num(u, 'to', Date.now());
    return sendText(res, JSON.stringify(reader.events(key, num(u, 'from', to - 24 * 3600_000), to, 10_000), null, 1), 'application/json', `events_${symbol}.json`);
  }
  if (p === '/api/config') {
    const { key } = params(u);
    if (req.method === 'GET') return json(res, 200, { config: hub.config(key), defaults: DEFAULT_DETECTOR_CONFIG });
    if (req.method === 'PUT' || req.method === 'POST') {
      const body = JSON.parse(await readBody(req));
      return json(res, 200, { config: hub.setConfig(key, body) });
    }
  }
  if (p === '/api/history' && req.method === 'DELETE') {
    const { key } = params(u);
    reader.clear(key);
    return json(res, 200, { ok: true });
  }
  if (p === '/api/perf') {
    const m = process.memoryUsage();
    return json(res, 200, {
      uptimeSec: Math.round(process.uptime()),
      memoryMB: { rss: +(m.rss / 1048576).toFixed(1), heapUsed: +(m.heapUsed / 1048576).toFixed(1), external: +(m.external / 1048576).toFixed(1) },
      // histogram samples include the 20 ms sampling interval itself; report the lag beyond it
      eventLoopLagMs: { p50: lag(loop.percentile(50)), p99: lag(loop.percentile(99)), max: lag(loop.max) },
      hub: hub.info(),
      db: reader.sizeInfo(),
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
    // keep-alive for proxies (Render closes idle connections)
    const iv = setInterval(() => ws.readyState === 1 && ws.ping(), 25_000);
    ws.on('close', () => clearInterval(iv));
  });
});

server.listen(PORT, () => {
  log(`OrderFlow Terminal listening on :${PORT} (db ${DB_PATH})`);
  for (const k of (process.env.DEFAULT_SYMBOLS ?? 'binance-futures:BTCUSDT').split(',').map((s) => s.trim()).filter(Boolean)) {
    const [src, sym] = k.split(':');
    if (!isSource(src) || !sym) continue;
    // keep retrying: the exchange may be unreachable at boot (network, geo-block, rate limit)
    const tryStart = (attempt: number): void => {
      hub.ensure(src, sym).catch((e) => {
        const delay = Math.min(300_000, 5000 * 2 ** attempt);
        log(`[${k}] could not start pinned session (retry in ${delay / 1000}s): ${e.message}`);
        setTimeout(() => tryStart(attempt + 1), delay);
      });
    };
    tryStart(0);
  }
});

const shutdown = () => {
  log('shutting down');
  hub.shutdown();
  setTimeout(() => {
    db.close();
    process.exit(0);
  }, 3500);
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
