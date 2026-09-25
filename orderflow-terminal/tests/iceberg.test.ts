import { harness } from './fixtures/sim.js';

/**
 * Sellers repeatedly hit the 100.0 bid. `hidden`: the displayed 3.0 does not shrink inside the same
 * depth batch (classic L2 iceberg signature). `visible`: the level is depleted and re-added in a later
 * diff, which is also exactly what ordinary new orders look like.
 */
function icebergRun(h: ReturnType<typeof harness>, seconds: number, mode: 'hidden' | 'visible' = 'hidden', display = 3) {
  h.sim.set('bid', 100.0, display);
  h.step([]);
  for (let i = 0; i < seconds * 5; i++) {
    if (mode === 'hidden') {
      h.step([[100.0, 1, -1, false]], 200);
      continue;
    }
    h.step([[100.0, 1, -1]], 200);
    if (h.sim.qty('bid', 100.0) <= 0) {
      h.sim.set('bid', 100.0, display);
      h.step([], 50);
    }
  }
}

describe('Iceberg detector', () => {
  it('flags a probable bid iceberg with confidence and explanation', () => {
    const h = harness();
    icebergRun(h, 15);
    const ice = h.events.filter((e) => e.kind === 'iceberg');
    expect(ice.length).toBe(1);
    const e = ice[0];
    expect(e.side).toBe('bid');
    expect(e.price).toBe(100);
    expect(e.confidence).toBeGreaterThanOrEqual(60);
    expect(e.confidence).toBeLessThanOrEqual(100);
    expect(e.title).toMatch(/Предполагаемый айсберг/);
    expect(['absorption_iceberg', 'replenishment_iceberg', 'probable_bid_iceberg']).toContain(e.subtype);
    expect(e.explain).toMatch(/НЕ точный размер скрытого остатка/);
    expect(e.explain).toMatch(/не калиброванная вероятность/);
    expect(Number(e.data?.estimatedHidden)).toBeGreaterThan(0);
    expect(Number(e.data?.refills)).toBeGreaterThanOrEqual(3);
  });

  it('visible replenishment alone is only "Признаки пополнения", never an iceberg', () => {
    const h = harness();
    icebergRun(h, 15, 'visible');
    expect(h.events.filter((e) => e.kind === 'iceberg')).toHaveLength(0);
    const rep = h.events.filter((e) => e.kind === 'replenishment');
    expect(rep).toHaveLength(1);
    expect(rep[0].explain).toMatch(/недостаточно/);
  });

  it('does not flag an ordinary large bid that is simply consumed (no refills)', () => {
    const h = harness();
    h.sim.set('bid', 100.0, 40);
    h.step([]);
    for (let i = 0; i < 60; i++) h.step([[100.0, 0.5, -1]], 200);
    expect(h.events.filter((e) => e.kind === 'iceberg')).toHaveLength(0);
    const cand = h.engine.iceberg.candidates(h.sim.t).find((c) => c.price === 100);
    expect(cand?.eligible).toBe(false);
  });

  it('needs enough evidence: a short burst is not reported', () => {
    const h = harness();
    icebergRun(h, 2);
    expect(h.events.filter((e) => e.kind === 'iceberg')).toHaveLength(0);
  });

  it('closes the estimate when the level is traded through', () => {
    const h = harness();
    icebergRun(h, 12);
    h.sim.set('bid', 100.0, 0);
    h.step([[99.9, 1, -1]], 100);
    const e = h.events.find((x) => x.kind === 'iceberg');
    expect(e?.status).toBe('broken');
  });

  it('emits nothing while the data gate is closed (stale / unsynced)', () => {
    const h = harness();
    h.engine.setGate(false);
    icebergRun(h, 15);
    expect(h.events).toHaveLength(0);
  });
});
