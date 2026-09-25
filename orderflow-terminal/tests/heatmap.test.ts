import { HeatmapRecorder, autoHeatStep, columnsToCsv, downsample, mergeColumns } from '../src/core/heatmap.js';
import { OrderBook, FlowClassifier } from '../src/core/orderbook.js';
import type { HeatColumn } from '../src/core/types.js';
import { harness } from './fixtures/sim.js';

describe('Heatmap aggregation', () => {
  it('snapshots resting liquidity per bucket and flow during the interval', () => {
    const b = new OrderBook(0.1);
    b.applySnapshot({ lastUpdateId: 1, t: 0, bids: [[100, 2], [99.9, 3], [99.5, 7]], asks: [[100.1, 1], [100.4, 4]] });
    const r = new HeatmapRecorder({ step: 0.5, halfBuckets: 4, intervalMs: 1000 }, 0.1);
    r.onTrade({ t: 1, price: 100, qty: 0.5, side: -1 });
    r.onTrade({ t: 2, price: 100.1, qty: 0.25, side: 1 });
    const f = new FlowClassifier(0.1);
    const recs = f.onDiff(3, b.applyDiff({ t: 3, firstId: 2, lastId: 2, prevLastId: 1, bids: [[99.5, 0]], asks: [[100.4, 6]] }));
    r.onFlow(recs);
    const c = r.snapshot(1000, b)!;
    expect(c.step).toBe(0.5);
    expect(c.n).toBe(9);
    // mid 100.05 -> bucket 200 (100.0-100.5); p0 = bucket 196 = 98.0
    expect(c.p0).toBe(98);
    const i100 = Math.round((100 - c.p0) / c.step);
    expect(c.bids[i100]).toBe(2); // only the 100.0 bid is in [100.0,100.5)
    expect(c.asks[i100]).toBe(1 + 6); // 100.1 + 100.4
    expect(c.bids[i100 - 1]).toBe(3); // 99.9 in [99.5,100.0); 99.5 removed
    expect(c.exec).toEqual([[i100, 0.25, 0.5]]);
    expect(c.add).toEqual([[i100, 2]]);
    expect(c.rem).toEqual([[i100 - 1, 7]]);
    expect(c.hi).toBe(100.1);
    expect(c.lo).toBe(100);
  });

  it('marks buckets outside the snapshot range as unknown (-1), never as empty', () => {
    const b = new OrderBook(0.1);
    b.applySnapshot({ lastUpdateId: 1, t: 0, bids: [[100, 2], [99.9, 3]], asks: [[100.1, 1], [100.2, 1]] });
    const r = new HeatmapRecorder({ step: 0.1, halfBuckets: 10, intervalMs: 1000 }, 0.1);
    const c = r.snapshot(1000, b)!;
    expect(c.bids[0]).toBe(-1);
    expect(c.asks[c.n - 1]).toBe(-1);
    expect(c.bids.filter((x) => x >= 0).length).toBe(4);
  });

  it('returns null without a two-sided book', () => {
    const r = new HeatmapRecorder({ step: 1, halfBuckets: 10, intervalMs: 1000 }, 0.1);
    expect(r.snapshot(1, new OrderBook(0.1))).toBeNull();
  });

  const col = (t: number, p0: number, bids: number[], exec: [number, number, number][] = []): HeatColumn => ({
    t, dt: 1000, p0, step: 1, n: bids.length, bids, asks: bids.map(() => 0), exec, add: [], rem: [], bb: 0, ba: 0, hi: 0, lo: 0, last: 0,
  });

  it('merges columns on a common grid with time-weighted averages and summed flow', () => {
    const m = mergeColumns([col(1000, 10, [2, 4], [[0, 1, 0]]), col(2000, 11, [6, 8], [[0, 2, 3]])]);
    expect(m.p0).toBe(10);
    expect(m.n).toBe(3);
    expect(m.bids).toEqual([2, 5, 8]);
    expect(m.exec).toEqual([[0, 1, 0], [1, 2, 3]]);
    expect(m.t).toBe(2000);
    expect(m.dt).toBe(2000);
  });

  it('downsamples to fixed buckets (retention tiers)', () => {
    const cols = Array.from({ length: 30 }, (_, i) => col(1000 * (i + 1), 10, [i, i]));
    const d = downsample(cols, 10_000);
    expect(d).toHaveLength(3);
    expect(d[0].bids[0]).toBeCloseTo(4.5, 10);
    expect(d[2].t).toBe(30_000);
  });

  it('exports CSV rows with UTC timestamps', () => {
    const csv = columnsToCsv([col(0, 10, [0, 3], [[1, 1, 2]])]);
    expect(csv.split('\n')).toEqual(['time_utc,price,bid_qty,ask_qty,buy_exec,sell_exec,added,cancelled', '1970-01-01T00:00:00.000Z,11,3,0,1,2,0,0']);
  });

  it('chooses a bucket size so the book range fits the target bucket count', () => {
    expect(autoHeatStep(0.1, 200, 400)).toBe(0.5);
    expect(autoHeatStep(0.01, 1, 400)).toBe(0.01);
  });

  it('engine produces one column per interval from the live book', () => {
    const h = harness();
    const cols: HeatColumn[] = [];
    (h.engine as unknown as { sink: { heat: (c: HeatColumn) => void } }).sink.heat = (c) => cols.push(c);
    h.idle(5000);
    expect(cols.length).toBeGreaterThanOrEqual(4);
    expect(cols[1].bb).toBe(100);
    expect(cols[1].ba).toBe(100.1);
  });
});
