import { BookSync, FlowClassifier, OfiTracker, OrderBook } from '../src/core/orderbook.js';
import type { DepthDiff } from '../src/core/types.js';

const snap = { lastUpdateId: 100, t: 0, bids: [[100, 2], [99.9, 3], [99.8, 1]] as [number, number][], asks: [[100.1, 1], [100.2, 4], [100.3, 2]] as [number, number][] };
const diff = (U: number, u: number, pu: number, bids: [number, number][] = [], asks: [number, number][] = [], t = 1): DepthDiff => ({ t, firstId: U, lastId: u, prevLastId: pu, bids, asks });

describe('OrderBook reconstruction', () => {
  it('applies snapshot and diffs, tracks best bid/ask, spread, microprice, OBI', () => {
    const b = new OrderBook(0.1);
    b.applySnapshot(snap);
    expect(b.bestBid).toBe(100);
    expect(b.bestAsk).toBe(100.1);
    const ch = b.applyDiff(diff(101, 101, 100, [[100, 0], [99.9, 5]], [[100.1, 3]]));
    expect(ch).toEqual([
      { side: 'bid', tick: 1000, oldQty: 2, newQty: 0 },
      { side: 'bid', tick: 999, oldQty: 3, newQty: 5 },
      { side: 'ask', tick: 1001, oldQty: 1, newQty: 3 },
    ]);
    expect(b.bestBid).toBe(99.9);
    const s = b.stats(10, 0, 1);
    expect(s.spread).toBeCloseTo(0.2, 10);
    // microprice = (bid*askQty + ask*bidQty)/(bidQty+askQty)
    expect(s.microprice).toBeCloseTo((99.9 * 3 + 100.1 * 5) / 8, 10);
    expect(s.obi).toBeCloseTo((6 - 9) / 15, 10);
    expect(b.top(2)).toEqual({ bids: [[99.9, 5], [99.8, 1]], asks: [[100.1, 3], [100.2, 4]] });
  });

  it('new best levels inside the spread update the touch', () => {
    const b = new OrderBook(0.1);
    b.applySnapshot(snap);
    b.applyDiff(diff(101, 101, 100, [[100.05, 0]], []));
    b.applyDiff(diff(102, 102, 101, [], [[100.1, 0]]));
    expect(b.bestAsk).toBe(100.2);
  });

  it('removes stale crossing levels', () => {
    const b = new OrderBook(0.1);
    b.applySnapshot(snap);
    b.applyDiff(diff(101, 101, 100, [[100.2, 9]], []));
    expect(b.bestBid).toBeLessThan(b.bestAsk);
  });

  it('prunes far levels and shrinks the reliable range', () => {
    const b = new OrderBook(0.1);
    const bids: [number, number][] = [];
    for (let i = 0; i < 100; i++) bids.push([+(100 - i * 0.1).toFixed(1), 1]);
    b.applySnapshot({ lastUpdateId: 1, t: 0, bids, asks: [[100.1, 1]] });
    const removed = b.prune(10);
    expect(removed).toBe(89);
    expect(b.reliableLo).toBe(990);
  });

  it('reports levels outside the snapshot range as unreliable', () => {
    const b = new OrderBook(0.1);
    b.applySnapshot(snap);
    b.applyDiff(diff(101, 101, 100, [[90, 7]], []));
    const seen: number[] = [];
    b.forEachInRange(0, 5000, (_s, k) => seen.push(k));
    expect(seen).not.toContain(900);
  });
});

describe('BookSync sequence validation (futures)', () => {
  it('buffers, bridges the snapshot, drops stale diffs', () => {
    const s = new BookSync('futures');
    s.reset();
    s.onDiff(diff(90, 95, 89));
    s.onDiff(diff(96, 99, 95));
    s.onDiff(diff(100, 104, 99));
    s.onDiff(diff(105, 107, 104));
    const r = s.onSnapshot(101);
    expect(r.gap).toBe(false);
    expect(r.applied.map((d) => d.lastId)).toEqual([104, 107]);
    expect(r.dropped).toBe(2);
    expect(s.state).toBe('synced');
    expect(s.onDiff(diff(108, 110, 107)).applied).toHaveLength(1);
  });

  it('detects a pu gap and requests resync', () => {
    const s = new BookSync('futures');
    s.reset();
    s.onDiff(diff(100, 104, 99));
    s.onSnapshot(101);
    const r = s.onDiff(diff(120, 125, 119));
    expect(r.gap).toBe(true);
    expect(r.reason).toMatch(/pu=119 expected 104/);
    expect(s.state).toBe('buffering');
    // the gap diff is kept to bridge the next snapshot
    const r2 = s.onSnapshot(122);
    expect(r2.gap).toBe(false);
    expect(r2.applied[0].lastId).toBe(125);
  });

  it('fails when the snapshot is newer than every buffered diff', () => {
    const s = new BookSync('futures');
    s.reset();
    s.onDiff(diff(100, 104, 99));
    const r = s.onSnapshot(90);
    expect(r.gap).toBe(true);
  });

  it('snapshot newer than every buffered diff: waits for the live diff that bridges it (seen live on Binance)', () => {
    const s = new BookSync('futures');
    s.reset();
    s.onDiff(diff(90, 95, 89));
    const r = s.onSnapshot(120);
    expect(r.gap).toBe(false);
    expect(r.applied).toHaveLength(0);
    expect(s.onDiff(diff(100, 110, 95)).applied).toHaveLength(0); // older than the snapshot: dropped, no false gap
    const b = s.onDiff(diff(111, 125, 110)); // contains 120: bridges the snapshot even though pu != 120
    expect(b.gap).toBe(false);
    expect(b.applied).toHaveLength(1);
    expect(s.onDiff(diff(126, 130, 125)).applied).toHaveLength(1);
  });

  it('a live diff that jumps past the snapshot without bridging it is a gap', () => {
    const s = new BookSync('futures');
    s.reset();
    s.onSnapshot(120);
    expect(s.onDiff(diff(130, 140, 129)).gap).toBe(true);
  });

  it('bounds the pre-snapshot buffer', () => {
    const s = new BookSync('futures', 10);
    s.reset();
    for (let i = 0; i < 25; i++) s.onDiff(diff(i * 2, i * 2 + 1, i * 2 - 1));
    expect(s.buffered).toBe(10);
    expect(s.dropped).toBe(15);
  });
});

describe('BookSync sequence validation (spot)', () => {
  it('uses U == prev u + 1 continuity', () => {
    const s = new BookSync('spot');
    s.reset();
    s.onDiff({ t: 1, firstId: 99, lastId: 102, bids: [], asks: [] });
    const r = s.onSnapshot(100);
    expect(r.applied).toHaveLength(1);
    expect(s.onDiff({ t: 2, firstId: 103, lastId: 105, bids: [], asks: [] }).gap).toBe(false);
    expect(s.onDiff({ t: 3, firstId: 107, lastId: 108, bids: [], asks: [] }).gap).toBe(true);
  });
});

describe('FlowClassifier add / cancel / execute', () => {
  const setup = () => {
    const b = new OrderBook(0.1);
    b.applySnapshot(snap);
    return { b, f: new FlowClassifier(0.1) };
  };

  it('attributes a depletion to trades first, the rest is a cancel', () => {
    const { b, f } = setup();
    f.onTrade({ t: 5, price: 100, qty: 0.5, side: -1 }, b);
    const ch = b.applyDiff(diff(101, 101, 100, [[100, 0]], [], 10));
    const [r] = f.onDiff(10, ch);
    expect(r).toMatchObject({ side: 'bid', executed: 0.5, cancelled: 1.5, added: 0, hidden: 0 });
  });

  it('volume beyond the visible depletion is hidden (refill)', () => {
    const { b, f } = setup();
    f.onTrade({ t: 5, price: 100.1, qty: 3, side: 1 }, b);
    const ch = b.applyDiff(diff(101, 101, 100, [], [[100.1, 1]], 10));
    const recs = f.onDiff(10, ch);
    // ask 100.1 unchanged at 1 -> no change record; trade fully hidden
    expect(recs).toEqual([{ t: 10, side: 'ask', tick: 1001, added: 0, cancelled: 0, executed: 0, hidden: 3, qty: NaN, prevQty: NaN }]);
  });

  it('keeps trades newer than the diff pending', () => {
    const { b, f } = setup();
    f.onTrade({ t: 20, price: 100, qty: 1, side: -1 }, b);
    const recs = f.onDiff(10, b.applyDiff(diff(101, 101, 100, [[99.9, 4]], [], 10)));
    expect(recs.find((r) => r.tick === 1000)).toBeUndefined();
  });

  it('re-classifies a cancel when the trade arrives late', () => {
    const { b, f } = setup();
    const ch = b.applyDiff(diff(101, 101, 100, [[100, 1]], [], 10));
    const [r] = f.onDiff(10, ch);
    expect(r.cancelled).toBe(1);
    const corr = f.onTrade({ t: 9, price: 100, qty: 1, side: -1 }, b);
    expect(corr).toHaveLength(1);
    expect(corr[0]).toMatchObject({ executed: 1, cancelled: -1, hidden: 0 });
  });
});

describe('OFI', () => {
  it('matches Cont-Kukanov-Stoikov increments', () => {
    const o = new OfiTracker(10_000);
    o.update(0, 100, 5, 101, 5);
    expect(o.update(1, 100, 7, 101, 5)).toBe(2); // bid size up
    expect(o.update(2, 100, 7, 101, 8)).toBe(-1); // ask size up
    expect(o.update(3, 100.5, 1, 101, 8)).toBe(0); // new better bid +1
    expect(o.update(20_000, 100.5, 1, 101, 8)).toBe(0); // window expired
  });
});
