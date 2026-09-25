import { Session } from '../src/server/session.js';
import { DEFAULT_DETECTOR_CONFIG } from '../src/core/detectors/config.js';
import { FakeVenue } from './fixtures/fakeVenue.js';
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

function setup(staleMs = 800) {
  const venue = new FakeVenue();
  const pubs: { ch: string; d: unknown }[] = [];
  const s = new Session(TEST_META, venue.adapter(), { ...DEFAULT_DETECTOR_CONFIG, staleMs }, { publish: (ch, d) => pubs.push({ ch, d }), staleMs, skipWarmup: true, heatHalfBuckets: 50 });
  let pump: NodeJS.Timeout | null = null;
  const startPump = () => {
    pump = setInterval(() => {
      venue.sim.t = Date.now();
      venue.sendDiff();
    }, 100);
  };
  const stopPump = () => pump && clearInterval(pump);
  const feed = () => pubs.filter((p) => p.ch === 'event' && (p.d as MarketEvent).kind === 'feed').map((p) => (p.d as MarketEvent).title);
  return { venue, s, pubs, startPump, stopPump, feed };
}

describe('Live session: sync, resync, stale protection, reconnect', () => {
  it('syncs the book from snapshot + buffered diffs and publishes real book data', async () => {
    const { venue, s, pubs, startPump, stopPump } = setup();
    startPump();
    await s.start();
    await until(() => s.state === 'connected');
    await until(() => pubs.some((p) => p.ch === 'book'));
    const book = pubs.find((p) => p.ch === 'book')!.d as { bids: [number, number][]; asks: [number, number][] };
    expect(book.bids[0]).toEqual([100, 3]);
    expect(book.asks[0][0]).toBe(100.1);
    expect(s.engine.gateOpen).toBe(true);
    expect(venue.snapshots).toBe(1);
    stopPump();
    s.stop();
    await venue.close();
  });

  it('detects a sequence gap, blocks signals, fetches a new snapshot and resyncs', async () => {
    const { venue, s, startPump, stopPump, feed } = setup();
    startPump();
    await s.start();
    await until(() => s.state === 'connected');
    // skip update ids -> pu mismatch
    venue.sim.lastId += 50;
    venue.sim.set('bid', 99.0, 9);
    await until(() => s.getStatus().gaps >= 1);
    await until(() => s.state === 'connected' && venue.snapshots >= 2);
    expect(feed()).toContain('Data gap');
    expect(feed()).toContain('Resynchronization');
    expect(s.engine.book.qtyAt('bid', 990)).toBe(9);
    stopPump();
    s.stop();
    await venue.close();
  });

  it('marks data stale when depth stops, closes the signal gate, recovers when data resumes', async () => {
    const { venue, s, startPump, stopPump, feed } = setup(600);
    startPump();
    await s.start();
    await until(() => s.state === 'connected');
    stopPump();
    await until(() => s.state === 'stale', 4000);
    expect(s.engine.gateOpen).toBe(false);
    expect(feed()).toContain('Stale data');
    startPump();
    await until(() => s.state === 'connected', 4000);
    expect(s.engine.gateOpen).toBe(true);
    stopPump();
    s.stop();
    await venue.close();
  });

  it('reconnects after a dropped socket and rebuilds the book', async () => {
    const { venue, s, startPump, stopPump, feed } = setup();
    startPump();
    await s.start();
    await until(() => s.state === 'connected');
    venue.kill();
    await until(() => s.state === 'reconnecting' || s.state === 'syncing');
    expect(s.engine.gateOpen).toBe(false);
    await until(() => s.state === 'connected' && venue.connections >= 2, 8000);
    expect(feed()).toContain('Order-book disconnect — reconnecting');
    expect(s.getStatus().reconnects).toBeGreaterThanOrEqual(1);
    stopPump();
    s.stop();
    await venue.close();
  });

  it('publishes trades, status with latency and counters', async () => {
    const { venue, s, pubs, startPump, stopPump } = setup();
    startPump();
    await s.start();
    await until(() => s.state === 'connected');
    venue.sendTrade({ t: Date.now(), price: 100, qty: 0.5, side: -1, id: 1 });
    await until(() => pubs.some((p) => p.ch === 'trades'));
    await until(() => pubs.some((p) => p.ch === 'status' && (p.d as { trades: number }).trades === 1));
    const st = [...pubs].reverse().find((p) => p.ch === 'status')!.d as { latencyMs: number; depthUpdates: number; synced: boolean };
    expect(st.synced).toBe(true);
    expect(st.depthUpdates).toBeGreaterThan(0);
    expect(Number.isFinite(st.latencyMs)).toBe(true);
    stopPump();
    s.stop();
    await venue.close();
  });
});
