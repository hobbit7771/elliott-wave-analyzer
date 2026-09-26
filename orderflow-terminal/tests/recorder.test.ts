import { HistoryReader, Recorder, openDb } from '../src/server/recorder.js';
import type { HeatColumn } from '../src/core/types.js';

const col = (t: number, v: number): HeatColumn => ({ t, dt: 1000, p0: 100, step: 1, n: 3, bids: [v, v, 0], asks: [0, 0, v], exec: [[1, v, 0]], add: [], rem: [], bb: 101, ba: 102, hi: 101, lo: 101, last: 101 });

describe('History recorder', () => {
  it('stores trades, heat columns, events, candles and reads them back', () => {
    const db = openDb(':memory:');
    const rec = new Recorder(db, 'x:A');
    const rd = new HistoryReader(db);
    const now = 1_000_000_000;
    for (let i = 0; i < 5; i++) rec.trade({ t: now + i, price: 100 + i, qty: 1, side: i % 2 ? 1 : -1, id: i });
    rec.trade({ t: now, price: 100, qty: 1, side: -1, id: 0 }); // duplicate id (backfill overlap) ignored
    rec.heat(col(now + 1000, 5));
    rec.event({ id: 'e1', t: now, kind: 'iceberg', title: 'Probable Bid Iceberg', price: 100, confidence: 70, explain: 'x', source: 'binance-futures', symbol: 'A' });
    rec.event({ id: 'e1', t: now, kind: 'iceberg', title: 'Probable Bid Iceberg', price: 100, confidence: 80, explain: 'y', source: 'binance-futures', symbol: 'A', status: 'broken' });
    rec.candle1m({ t: now, o: 1, h: 2, l: 0.5, c: 1.5, v: 10, bv: 6, n: 3 });
    rec.flush(now + 2000);
    expect(rd.trades('x:A', now, now + 10)).toHaveLength(5);
    expect(rd.trades('x:A', now, now + 10)[1]).toEqual({ t: now + 1, price: 101, qty: 1, side: 1, id: 1 });
    const ev = rd.events('x:A', 0, now * 2);
    expect(ev).toHaveLength(1);
    expect(ev[0].confidence).toBe(80);
    expect(rd.heat('x:A', now, now + 5000).cols[0].bids).toEqual([5, 5, 0]);
    expect(rd.candles1m('x:A', 0, now * 2)[0].bv).toBe(6);
    expect(rd.trades('y:B', 0, now * 2)).toHaveLength(0); // per-instrument isolation
  });

  it('aggregates 1s columns into 10s / 60s tiers and applies retention', () => {
    const db = openDb(':memory:');
    const ret = { heatRawMs: 60_000, heat10Ms: 300_000, heat60Ms: 3600_000, tradesMs: 60_000, eventsMs: 3600_000 };
    const rec = new Recorder(db, 'x:A', ret);
    const rd = new HistoryReader(db);
    const base = 1_700_000_040_000; // aligned to 10 s and 60 s
    for (let s = 1; s <= 600; s++) {
      rec.heat(col(base + s * 1000, s));
      rec.trade({ t: base + s * 1000, price: 100, qty: 1, side: 1, id: s });
      if (s % 10 === 0) rec.flush(base + s * 1000 + 1);
    }
    rec.flush(base + 601_000);
    const n = (res: number) => (db.prepare('SELECT COUNT(*) n FROM heat WHERE res=?').get(res) as { n: number }).n;
    // raw tier keeps ~60 s (+ up to one 60 s purge interval); the 10 s tier covers the rest
    expect(n(1)).toBeLessThanOrEqual(121);
    expect(n(10)).toBeGreaterThanOrEqual(29); // 300 s retention
    expect(n(10)).toBeLessThanOrEqual(36);
    expect(n(60)).toBeGreaterThanOrEqual(8);
    const tenth = db.prepare('SELECT data FROM heat WHERE res=10 ORDER BY t LIMIT 1').get();
    expect(tenth).toBeTruthy();
    expect(rd.trades('x:A', 0, base * 2).length).toBeLessThanOrEqual(121);
    // reading an old range falls back to the coarse tier; recent range uses 1 s
    const old = rd.heat('x:A', base + 330_000, base + 500_000, 1000);
    const oldest = rd.heat('x:A', base + 10_000, base + 600_000, 1000);
    expect(oldest.res).toBe(60);
    expect(old.res).toBe(10);
    expect(old.cols.length).toBeGreaterThan(15);
    const recent = rd.heat('x:A', base + 560_000, base + 600_000, 1000);
    expect(recent.res).toBe(1);
    expect(recent.cols.at(-1)!.bids[0]).toBe(600);
  });

  it('uses the 1 s tier for a fresh recording even when the requested window starts earlier', () => {
    const db = openDb(':memory:');
    const rec = new Recorder(db, 'x:A');
    const rd = new HistoryReader(db);
    const base = 1_700_000_040_000;
    for (let s = 1; s <= 25; s++) {
      rec.heat(col(base + s * 1000, s));
      rec.flush(base + s * 1000 + 1);
    }
    const r = rd.heat('x:A', base - 900_000, base + 26_000, 1500);
    expect(r.res).toBe(1);
    expect(r.cols).toHaveLength(25);
  });

  it('never decodes more than ~maxCols columns for a long range (thinned evenly in SQL)', () => {
    const db = openDb(':memory:');
    const rec = new Recorder(db, 'x:A');
    const rd = new HistoryReader(db);
    const base = 1_700_000_040_000;
    for (let s = 1; s <= 900; s++) rec.heat(col(base + s * 1000, s));
    rec.flush(base + 901_000);
    // 900 one-second columns for maxCols 400: the 1 s tier is chosen (span < 3 x maxCols) and thinned
    const r = rd.heat('x:A', base, base + 901_000, 400);
    expect(r.res).toBe(1);
    expect(r.cols.length).toBeLessThanOrEqual(400);
    expect(r.cols.length).toBeGreaterThanOrEqual(250);
    // evenly spread over the whole range, oldest first
    expect(r.cols[0].t).toBeLessThan(base + 20_000);
    expect(r.cols.at(-1)!.t).toBeGreaterThan(base + 850_000);
    for (let i = 1; i < r.cols.length; i++) expect(r.cols[i].t).toBeGreaterThan(r.cols[i - 1].t);
  });

  it('clears one instrument\'s history', () => {
    const db = openDb(':memory:');
    const rec = new Recorder(db, 'x:A');
    rec.trade({ t: 1, price: 1, qty: 1, side: 1 });
    rec.flush(2);
    const rd = new HistoryReader(db);
    rd.clear('x:A');
    expect(rd.trades('x:A', 0, 10)).toHaveLength(0);
  });

  it('persists per-instrument settings', () => {
    const rd = new HistoryReader(openDb(':memory:'));
    rd.setSetting('cfg:x', { iceberg: { minRefills: 5 } });
    expect(rd.getSetting('cfg:x')).toEqual({ iceberg: { minRefills: 5 } });
  });
});
