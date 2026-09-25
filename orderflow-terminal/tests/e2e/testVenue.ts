// TEST-ONLY local venue speaking the Binance USD-M futures REST + combined-stream protocol,
// driven by the deterministic SimExchange. Lets the real server + real browser UI be exercised
// end-to-end in environments without exchange access. Never used by production code.
import http from 'node:http';
import { WebSocketServer, type WebSocket } from 'ws';
import { createServer, type AddressInfo } from 'node:net';
import { SimExchange } from '../fixtures/sim.js';
import type { Candle, Trade } from '../../src/core/types.js';

export interface TestVenue {
  url: string;
  wsUrl: string;
  close(): Promise<void>;
}

/** Deterministic pseudo-sequence (LCG) for the scenario — tests only. */
function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296);
}

export async function startTestVenue(port = 0): Promise<TestVenue> {
  const sim = new SimExchange(0.1);
  sim.t = Date.now();
  sim.seed(100, 400);
  const next = lcg(42);
  const trades: Trade[] = [];
  const sockets = new Set<WebSocket>();
  const start = Date.now();
  let mid = 100.0;

  // pre-history for klines: flat-ish deterministic candles before the venue started
  const history: Candle[] = [];
  for (let i = 300; i > 0; i--) {
    const t = Math.floor(start / 60_000) * 60_000 - i * 60_000;
    const o = 100 + Math.sin(i / 7) * 0.8;
    const c = 100 + Math.sin((i - 1) / 7) * 0.8;
    history.push({ t, o, h: Math.max(o, c) + 0.3, l: Math.min(o, c) - 0.3, c, v: 50, bv: 25 + Math.sin(i / 3) * 10, n: 100 });
  }

  const send = (stream: string, data: unknown) => {
    const s = JSON.stringify({ stream, data });
    for (const ws of sockets) if (ws.readyState === 1) ws.send(s);
  };

  const icebergPx = 99.5;
  const step = () => {
    const now = Date.now();
    sim.t = now;
    const phase = (now - start) / 1000;
    // drift the touch deterministically
    const target = 100 + Math.sin(phase / 25 + 4.3) * 1.2 + (next() - 0.5) * 0.2;
    const bestBid = +(Math.round(Math.max(icebergPx, Math.min(target, 101.5)) * 10) / 10).toFixed(1);
    if (Math.abs(bestBid - mid) >= 0.1) {
      // shift the book: clear crossing levels and fill the new touch
      for (let k = sim.k(Math.min(mid, bestBid)) - 2; k <= sim.k(Math.max(mid, bestBid)) + 3; k++) {
        const p = sim.p(k);
        if (p <= bestBid) {
          if (!sim.qty('bid', p)) sim.set('bid', p, 2 + Math.round(next() * 5));
          if (sim.qty('ask', p)) sim.set('ask', p, 0);
        } else {
          if (!sim.qty('ask', p)) sim.set('ask', p, 2 + Math.round(next() * 5));
          if (sim.qty('bid', p)) sim.set('bid', p, 0);
        }
      }
      mid = bestBid;
    }
    // occasional large resting orders
    if (Math.floor(phase) % 40 === 5) sim.set('ask', +(mid + 1.2).toFixed(1), 60);
    if (Math.floor(phase) % 40 === 30) sim.set('ask', +(mid + 1.2).toFixed(1), 3);
    // aggressive flow
    const n = 1 + Math.floor(next() * 4);
    for (let i = 0; i < n; i++) {
      const buy = next() < 0.5;
      const bb = [...sim.bids.keys()].reduce((a, b) => Math.max(a, b), -Infinity);
      const ba = [...sim.asks.keys()].reduce((a, b) => Math.min(a, b), Infinity);
      const price = buy ? sim.p(ba) : sim.p(bb);
      const qty = +(0.1 + next() * 1.2).toFixed(3);
      const tr = sim.trade(price, qty, buy ? 1 : -1);
      trades.push(tr);
      send('testusdt@aggTrade', { e: 'aggTrade', E: now, a: tr.id, s: 'TESTUSDT', p: String(tr.price), q: String(tr.qty), T: tr.t, m: !buy });
      // normal replenishment of the touch, and an iceberg that always refills at icebergPx
      const passive = buy ? 'ask' : 'bid';
      if (sim.qty(passive, price) <= 0) sim.set(passive, price, price === icebergPx ? 3 : 1 + Math.round(next() * 4));
    }
    if (trades.length > 200_000) trades.splice(0, 50_000);
    const d = sim.diff();
    send('testusdt@depth@100ms', { e: 'depthUpdate', E: now, T: now, s: 'TESTUSDT', U: d.firstId, u: d.lastId, pu: d.prevLastId, b: d.bids.map(([p, q]) => [String(p), String(q)]), a: d.asks.map(([p, q]) => [String(p), String(q)]) });
    const bb = [...sim.bids.keys()].reduce((a, b) => Math.max(a, b), -Infinity);
    const ba = [...sim.asks.keys()].reduce((a, b) => Math.min(a, b), Infinity);
    send('testusdt@bookTicker', { e: 'bookTicker', u: d.lastId, E: now, T: now, s: 'TESTUSDT', b: String(sim.p(bb)), B: String(sim.bids.get(bb)), a: String(sim.p(ba)), A: String(sim.asks.get(ba)) });
  };
  const timer = setInterval(step, 100);
  const markTimer = setInterval(() => {
    const now = Date.now();
    send('testusdt@markPrice@1s', { e: 'markPriceUpdate', E: now, s: 'TESTUSDT', p: String(mid), i: String(mid), P: String(mid), r: '0.00010000', T: Math.ceil(now / 28_800_000) * 28_800_000 });
  }, 1000);

  const klines = (interval: string, limit: number, end?: number): unknown[] => {
    const ms = { '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000, '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000 }[interval] ?? 60_000;
    const map = new Map<number, Candle>();
    for (const c of history) {
      const t = Math.floor(c.t / ms) * ms;
      const x = map.get(t);
      if (!x) map.set(t, { ...c, t });
      else Object.assign(x, { h: Math.max(x.h, c.h), l: Math.min(x.l, c.l), c: c.c, v: x.v + c.v, bv: x.bv + c.bv });
    }
    for (const tr of trades) {
      const t = Math.floor(tr.t / ms) * ms;
      const x = map.get(t);
      if (!x) map.set(t, { t, o: tr.price, h: tr.price, l: tr.price, c: tr.price, v: tr.qty, bv: tr.side === 1 ? tr.qty : 0, n: 1 });
      else Object.assign(x, { h: Math.max(x.h, tr.price), l: Math.min(x.l, tr.price), c: tr.price, v: x.v + tr.qty, bv: x.bv + (tr.side === 1 ? tr.qty : 0), n: (x.n ?? 0) + 1 });
    }
    return [...map.values()]
      .filter((c) => !end || c.t <= end)
      .sort((a, b) => a.t - b.t)
      .slice(-limit)
      .map((c) => [c.t, String(c.o), String(c.h), String(c.l), String(c.c), String(c.v), c.t + ms - 1, '0', c.n ?? 0, String(c.bv), '0', '0']);
  };

  const server = http.createServer((req, res) => {
    const u = new URL(req.url ?? '/', 'http://x');
    const json = (b: unknown) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(b));
    };
    switch (u.pathname) {
      case '/fapi/v1/exchangeInfo':
        return json({ symbols: [{ symbol: 'TESTUSDT', status: 'TRADING', baseAsset: 'TEST', quoteAsset: 'USDT', contractType: 'PERPETUAL', filters: [{ filterType: 'PRICE_FILTER', tickSize: '0.10' }, { filterType: 'LOT_SIZE', stepSize: '0.001' }] }] });
      case '/fapi/v1/depth': {
        const s = sim.snapshot();
        return json({ lastUpdateId: s.lastUpdateId, E: Date.now(), T: Date.now(), bids: s.bids.map(([p, q]) => [String(p), String(q)]), asks: s.asks.map(([p, q]) => [String(p), String(q)]) });
      }
      case '/fapi/v1/klines':
        return json(klines(u.searchParams.get('interval') ?? '1m', +(u.searchParams.get('limit') ?? 500), u.searchParams.get('endTime') ? +u.searchParams.get('endTime')! : undefined));
      case '/fapi/v1/aggTrades': {
        const a = +(u.searchParams.get('startTime') ?? 0);
        const b = +(u.searchParams.get('endTime') ?? Date.now());
        return json(trades.filter((t) => t.t >= a && t.t <= b).slice(0, 1000).map((t) => ({ a: t.id, p: String(t.price), q: String(t.qty), T: t.t, m: t.side === -1 })));
      }
      case '/fapi/v1/openInterest':
        return json({ openInterest: '12345.6', time: Date.now() });
      default:
        res.writeHead(404).end();
    }
  });
  const wss = new WebSocketServer({ server, path: '/stream' });
  wss.on('connection', (ws) => {
    sockets.add(ws);
    ws.on('close', () => sockets.delete(ws));
  });
  await new Promise<void>((r) => server.listen(port, '127.0.0.1', () => r()));
  const p = (server.address() as AddressInfo).port;
  return {
    url: `http://127.0.0.1:${p}`,
    wsUrl: `ws://127.0.0.1:${p}/stream`,
    close: async () => {
      clearInterval(timer);
      clearInterval(markTimer);
      for (const s of sockets) s.terminate();
      await new Promise((r) => wss.close(() => server.close(() => r(null))));
    },
  };
}

/** A currently free local TCP port. */
export async function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const srv = createServer();
    srv.listen(0, '127.0.0.1', () => {
      const p = (srv.address() as AddressInfo).port;
      srv.close(() => resolve(p));
    });
  });
}
