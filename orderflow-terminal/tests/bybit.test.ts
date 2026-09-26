// TEST-ONLY local stand-in for the Bybit v5 public WebSocket (same message layout as the documentation).
import { FakeBybit } from './fixtures/fakeBybit.js';
import { Session } from '../src/server/session.js';
import { DEFAULT_DETECTOR_CONFIG } from '../src/core/detectors/config.js';
import { TEST_META } from './fixtures/sim.js';
import type { MarketEvent } from '../src/core/types.js';

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function until(fn: () => boolean, ms = 8000): Promise<void> {
  const t = Date.now();
  while (!fn()) {
    if (Date.now() - t > ms) throw new Error('timeout');
    await wait(20);
  }
}

describe('Bybit adapter (format + session sync, local stand-in; not verified against the live exchange here)', () => {
  it('parses snapshot / delta / trades / ticker deltas / liquidations', async () => {
    const { BybitLinearAdapter } = await import('../src/server/adapters/bybit.js');
    const a = new BybitLinearAdapter();
    const snap = a.parse(JSON.stringify({ topic: 'orderbook.200.X', type: 'snapshot', ts: 5, data: { b: [['1', '2']], a: [['1.1', '3']], u: 7 } }))[0];
    expect(snap).toMatchObject({ kind: 'snapshot', snap: { lastUpdateId: 7, bids: [[1, 2]], asks: [[1.1, 3]] } });
    const d = a.parse(JSON.stringify({ topic: 'orderbook.200.X', type: 'delta', ts: 6, data: { b: [['1', '0']], a: [], u: 8 } }))[0];
    expect(d).toMatchObject({ kind: 'diff', d: { firstId: 8, lastId: 8, bids: [[1, 0]] } });
    const tr = a.parse(JSON.stringify({ topic: 'publicTrade.X', data: [{ T: 9, S: 'Sell', v: '0.5', p: '1.05' }] }))[0];
    expect(tr).toMatchObject({ kind: 'trade', tr: { t: 9, price: 1.05, qty: 0.5, side: -1 } });
    a.parse(JSON.stringify({ topic: 'tickers.X', type: 'snapshot', ts: 1, data: { symbol: 'X', markPrice: '1.0', indexPrice: '1.01', fundingRate: '0.0001', nextFundingTime: '100' } }));
    const tk = a.parse(JSON.stringify({ topic: 'tickers.X', type: 'delta', ts: 2, data: { symbol: 'X', markPrice: '1.2' } }))[0];
    expect(tk).toMatchObject({ kind: 'mark', mark: 1.2, index: 1.01, funding: 0.0001 });
    const lq = a.parse(JSON.stringify({ topic: 'allLiquidation.X', data: [{ T: 3, S: 'Buy', v: '2', p: '0.9' }] }))[0];
    expect(lq).toMatchObject({ kind: 'liq', side: 'sell', price: 0.9, qty: 2 });
  });

  it('syncs from the stream snapshot, applies +1 deltas, and on a gap reconnects for a fresh snapshot', async () => {
    const venue = new FakeBybit();
    await venue.listen();
    process.env.BYBIT_WS = venue.url;
    const { BybitLinearAdapter } = await import('../src/server/adapters/bybit.js');
    const adapter = new BybitLinearAdapter();
    const pubs: { ch: string; d: unknown }[] = [];
    const s = new Session({ ...TEST_META, source: 'bybit-linear' }, adapter, { ...DEFAULT_DETECTOR_CONFIG, staleMs: 5000 }, { publish: (ch, d) => pubs.push({ ch, d }), staleMs: 5000, skipWarmup: true, heatHalfBuckets: 50 });
    const pump = setInterval(() => venue.delta([['100.0', String(3 + Math.random())]], []), 100);
    await s.start();
    await until(() => s.state === 'connected');
    expect(venue.subscribes).toEqual(expect.arrayContaining(['orderbook.200.TESTUSDT', 'publicTrade.TESTUSDT', 'tickers.TESTUSDT', 'allLiquidation.TESTUSDT']));
    await until(() => pubs.some((p) => p.ch === 'book'));
    const book = pubs.filter((p) => p.ch === 'book').pop()!.d as { bids: [number, number][]; asks: [number, number][] };
    expect(book.asks[0]).toEqual([100.1, 4]);
    venue.trade('Buy', '100.1', '0.7');
    await until(() => s.getStatus().trades >= 1);
    // skip an update id -> gap -> reconnect -> new stream snapshot -> synced again
    venue.delta([['99.9', '9']], [], 5);
    await until(() => s.getStatus().gaps >= 1);
    await until(() => s.state === 'connected' && venue.snapshots >= 2);
    const feed = pubs.filter((p) => p.ch === 'event' && (p.d as MarketEvent).kind === 'feed').map((p) => (p.d as MarketEvent).title);
    expect(feed).toContain('Разрыв данных');
    clearInterval(pump);
    await s.stop();
    await venue.close();
    delete process.env.BYBIT_WS;
  });
});
