// TEST-ONLY local stand-in for the Bybit v5 public API: WebSocket (orderbook.200 snapshot + deltas, publicTrade)
// and the REST endpoints a session uses, in the documented message layout.
import http from 'node:http';
import { WebSocketServer, type WebSocket } from 'ws';
import type { AddressInfo } from 'node:net';

export class FakeBybit {
  wss = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  rest = http.createServer((req, res) => this.onRest(req, res));
  books: WebSocket[] = [];
  flows: WebSocket[] = [];
  u = 1000;
  snapshots = 0;
  subscribes: string[] = [];
  pings = 0;
  symbol: string;
  constructor(symbol = 'TESTUSDT') {
    this.symbol = symbol;
    this.wss.on('connection', (ws) => {
      ws.on('message', (raw) => {
        const m = JSON.parse(String(raw)) as { op: string; args?: string[] };
        if (m.op === 'ping') {
          this.pings++;
          ws.send(JSON.stringify({ success: true, ret_msg: 'pong', op: 'ping' }));
          return;
        }
        if (m.op !== 'subscribe') return;
        this.subscribes.push(...(m.args ?? []));
        ws.send(JSON.stringify({ success: true, op: 'subscribe' }));
        if (m.args?.some((a) => a.startsWith('orderbook.'))) {
          this.books.push(ws);
          this.snapshots++;
          ws.send(JSON.stringify({ topic: `orderbook.200.${this.symbol}`, type: 'snapshot', ts: Date.now(), data: { s: this.symbol, b: [['100.0', '3'], ['99.9', '5']], a: [['100.1', '4'], ['100.2', '6']], u: this.u, seq: 1 } }));
        } else this.flows.push(ws);
      });
    });
  }
  async listen(): Promise<void> {
    await new Promise<void>((r) => (this.wss.address() ? r() : this.wss.once('listening', () => r())));
    await new Promise<void>((r) => this.rest.listen(0, '127.0.0.1', () => r()));
  }
  get url(): string {
    return `ws://127.0.0.1:${(this.wss.address() as AddressInfo).port}`;
  }
  get restUrl(): string {
    return `http://127.0.0.1:${(this.rest.address() as AddressInfo).port}`;
  }
  private onRest(req: http.IncomingMessage, res: http.ServerResponse): void {
    const u = new URL(req.url ?? '/', 'http://x');
    const ok = (result: unknown) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ retCode: 0, retMsg: 'OK', result }));
    };
    const now = Date.now();
    switch (u.pathname) {
      case '/v5/market/instruments-info':
        return ok({ list: [{ symbol: this.symbol, status: 'Trading', contractType: 'LinearPerpetual', baseCoin: 'TEST', quoteCoin: 'USDT', priceFilter: { tickSize: '0.1' }, lotSizeFilter: { qtyStep: '0.001' } }] });
      case '/v5/market/orderbook':
        return ok({ s: this.symbol, b: [['100.0', '3']], a: [['100.1', '4']], u: this.u, ts: now });
      case '/v5/market/kline': {
        const iv = u.searchParams.get('interval') ?? '1';
        const ms = iv === 'D' ? 86_400_000 : +iv * 60_000;
        const n = Math.min(200, +(u.searchParams.get('limit') ?? 200));
        const last = Math.floor(now / ms) * ms;
        // newest first, like the venue
        return ok({ list: Array.from({ length: n }, (_, i) => [String(last - i * ms), '100', '100.2', '99.9', '100.1', '10', '1000']) });
      }
      case '/v5/market/recent-trade':
        return ok({ list: [] });
      case '/v5/market/open-interest':
        return ok({ list: [{ openInterest: '1000', timestamp: String(now) }] });
      case '/v5/market/tickers':
        return ok({ list: [{ symbol: this.symbol, lastPrice: '100.1' }] });
      default:
        res.writeHead(404);
        res.end();
    }
  }
  delta(b: [string, string][], a: [string, string][], skip = 0): void {
    this.u += 1 + skip;
    const msg = JSON.stringify({ topic: `orderbook.200.${this.symbol}`, type: 'delta', ts: Date.now(), data: { s: this.symbol, b, a, u: this.u, seq: this.u } });
    for (const ws of this.books) if (ws.readyState === 1) ws.send(msg);
  }
  trade(side: 'Buy' | 'Sell', p: string, v: string): void {
    const msg = JSON.stringify({ topic: `publicTrade.${this.symbol}`, type: 'snapshot', ts: Date.now(), data: [{ T: Date.now(), s: this.symbol, S: side, v, p, L: 'PlusTick', i: String(Math.random()), BT: false }] });
    for (const ws of this.flows) if (ws.readyState === 1) ws.send(msg);
  }
  close(): Promise<void> {
    for (const c of this.wss.clients) c.terminate();
    this.rest.close();
    return new Promise((r) => this.wss.close(() => r()));
  }
}
