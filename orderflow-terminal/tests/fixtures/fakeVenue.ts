// TEST-ONLY local WebSocket "venue" that speaks the Binance futures combined-stream format,
// backed by the deterministic SimExchange. Used to test reconnect / resync / stale handling end-to-end.
import { WebSocketServer, type WebSocket } from 'ws';
import type { AddressInfo } from 'node:net';
import { BinanceFuturesAdapter } from '../../src/server/adapters/binance.js';
import type { BookSnapshot, Candle, DepthDiff, InstrumentMeta, Trade } from '../../src/core/types.js';
import type { Timeframe } from '../../src/core/candles.js';
import { SimExchange, flatCandles, TEST_META } from './sim.js';

export class FakeVenue {
  wss: WebSocketServer;
  sim = new SimExchange(0.1);
  sockets = new Set<WebSocket>();
  depthSockets = new Set<WebSocket>();
  flowSockets = new Set<WebSocket>();
  snapshots = 0;
  connections = 0;
  constructor() {
    this.sim.seed(100);
    this.sim.t = Date.now();
    this.wss = new WebSocketServer({ port: 0 });
    this.wss.on('connection', (ws, req) => {
      this.connections++;
      this.sockets.add(ws);
      // route-aware like Binance: /public carries depth + bookTicker, /market carries trades
      const set = (req.url ?? '').startsWith('/market') ? this.flowSockets : this.depthSockets;
      set.add(ws);
      ws.on('close', () => (this.sockets.delete(ws), set.delete(ws)));
    });
  }
  get url(): string {
    return `ws://127.0.0.1:${(this.wss.address() as AddressInfo).port}`;
  }
  sendDiff(d: DepthDiff = this.sim.diff()): void {
    const msg = JSON.stringify({ stream: 'testusdt@depth@100ms', data: { e: 'depthUpdate', E: Date.now(), T: d.t, s: 'TESTUSDT', U: d.firstId, u: d.lastId, pu: d.prevLastId, b: d.bids.map(([p, q]) => [String(p), String(q)]), a: d.asks.map(([p, q]) => [String(p), String(q)]) } });
    for (const s of this.depthSockets) s.send(msg);
    // keep the trades route alive like the real /market route does (markPrice every second)
    const mk = JSON.stringify({ stream: 'testusdt@markPrice@1s', data: { e: 'markPriceUpdate', E: Date.now(), s: 'TESTUSDT', p: '100', i: '100', P: '100', r: '0.0001', T: Date.now() + 3600_000 } });
    for (const s of this.flowSockets) s.send(mk);
  }
  sendTrade(t: Trade): void {
    const msg = JSON.stringify({ stream: 'testusdt@aggTrade', data: { e: 'aggTrade', E: Date.now(), a: t.id, s: 'TESTUSDT', p: String(t.price), q: String(t.qty), T: t.t, m: t.side === -1 } });
    for (const s of this.flowSockets) s.send(msg);
  }
  /** drop every client connection abruptly */
  kill(): void {
    for (const s of this.sockets) s.terminate();
  }
  close(): Promise<void> {
    this.kill();
    return new Promise((r) => this.wss.close(() => r()));
  }
  adapter(): BinanceFuturesAdapter {
    const v = this;
    return new (class extends BinanceFuturesAdapter {
      override streamRoutes() {
        return [
          { route: 'depth' as const, url: `${v.url}/public/stream?streams=testusdt@depth@100ms` },
          { route: 'flow' as const, url: `${v.url}/market/stream?streams=testusdt@aggTrade` },
        ];
      }
      override async fetchSnapshot(): Promise<BookSnapshot> {
        v.snapshots++;
        return v.sim.snapshot();
      }
      override async fetchKlines(_s: string, _tf: Timeframe): Promise<Candle[]> {
        return flatCandles(100, 100, 1, Date.now());
      }
      override async fetchAggTrades(): Promise<Trade[]> {
        return [];
      }
      override async listInstruments(): Promise<InstrumentMeta[]> {
        return [TEST_META];
      }
    })();
  }
}
