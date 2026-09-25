import { harness } from './fixtures/sim.js';

describe('Large limit order detector (dynamic threshold)', () => {
  it('stays silent until enough level-size samples exist (warm-up)', () => {
    const h = harness();
    h.sim.set('bid', 99.5, 60);
    h.idle(5000);
    expect(h.engine.large.thr.warm).toBe(false);
    expect(h.events.filter((e) => e.kind === 'large_order')).toHaveLength(0);
  });

  it('detects a level above max(%depth, percentile) held for minHold, with full lifecycle fields', () => {
    const h = harness();
    h.idle(25_000);
    const thr = h.engine.large.thr;
    expect(thr.warm).toBe(true);
    // pattern sizes are 2..6 -> P97 ~ 6; 2% of ~80 depth ~ 1.6
    expect(thr.bid).toBeGreaterThanOrEqual(5);
    expect(thr.bid).toBeLessThan(10);
    h.sim.set('bid', 99.5, 30);
    h.idle(2000);
    expect(h.events.filter((e) => e.kind === 'large_order')).toHaveLength(0); // not held long enough yet
    h.idle(2000);
    const ev = h.events.filter((e) => e.kind === 'large_order');
    expect(ev).toHaveLength(1);
    expect(ev[0]).toMatchObject({ side: 'bid', price: 99.5, title: 'Крупный уровень видимой ликвидности' });
    expect(ev[0].explain).toMatch(/Порог/);
    const lo = h.engine.large.list(h.sim.t).find((x) => x.price === 99.5)!;
    expect(lo).toMatchObject({ side: 'bid', size: 30, status: 'active', executed: 0, cancelled: 0, source: 'binance-futures' });
    expect(lo.holdMs).toBeGreaterThanOrEqual(3000);
    expect(lo.confidence).toBeGreaterThan(0);
  });

  it('marks a pulled order and reports Major Liquidity Removed', () => {
    const h = harness();
    h.idle(25_000);
    h.sim.set('ask', 100.6, 40);
    h.idle(4000);
    h.sim.set('ask', 100.6, 0);
    h.idle(1000);
    const lo = h.engine.large.list(h.sim.t).find((x) => x.price === 100.6)!;
    expect(lo.status).toBe('pulled');
    expect(lo.cancelled).toBe(40);
    expect(h.events.some((e) => e.kind === 'liquidity_pulled' && e.price === 100.6)).toBe(true);
  });

  it('flags spoofing suspicion for repeated large appear/cancel cycles, never as fact', () => {
    const h = harness();
    h.idle(25_000);
    for (let i = 0; i < 3; i++) {
      h.sim.set('ask', 100.6, 40);
      h.idle(4000);
      h.sim.set('ask', 100.6, 0);
      h.idle(2000);
    }
    const sp = h.events.filter((e) => e.kind === 'spoofing');
    expect(sp.length).toBeGreaterThanOrEqual(1);
    expect(sp[0].title).toMatch(/Подозрение/);
    expect(sp[0].explain).toMatch(/cannot be proven/);
    expect(h.engine.large.spoofed.has('a1006')).toBe(true);
  });

  it('a filled order is not called spoofing', () => {
    const h = harness();
    h.idle(25_000);
    h.sim.set('bid', 100.0, 20);
    h.idle(4000);
    for (let i = 0; i < 20; i++) h.step([[100.0, 1, -1]], 100);
    h.idle(1000);
    expect(h.events.some((e) => e.kind === 'spoofing')).toBe(false);
    const lo = h.engine.large.list(h.sim.t).find((x) => x.price === 100)!;
    expect(['filled', 'partially_filled', 'broken']).toContain(lo.status);
    expect(lo.executed).toBeGreaterThanOrEqual(15);
  });

  it('spoof-flagged levels are excluded from iceberg detection', () => {
    const h = harness();
    h.idle(25_000);
    for (let i = 0; i < 3; i++) {
      h.sim.set('bid', 100.0, 40);
      h.idle(4000);
      h.sim.set('bid', 100.0, 0);
      h.idle(2000);
    }
    h.sim.set('bid', 100.0, 3);
    h.step([]);
    for (let i = 0; i < 75; i++) {
      h.step([[100.0, 1, -1]], 200);
      if (h.sim.qty('bid', 100) <= 0) {
        h.sim.set('bid', 100, 3);
        h.step([], 50);
      }
    }
    expect(h.events.some((e) => e.kind === 'iceberg')).toBe(false);
    const c = h.engine.iceberg.candidates(h.sim.t).find((x) => x.price === 100);
    expect(c?.eligible).toBe(false);
  });
});
