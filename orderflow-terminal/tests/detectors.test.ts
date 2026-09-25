import { harness } from './fixtures/sim.js';

/** light two-sided baseline flow that does not move the book */
function baseline(h: ReturnType<typeof harness>, ms: number) {
  for (let x = 0; x < ms; x += 500) h.step([[100.0, 0.2, -1, false], [100.1, 0.2, 1, false]], 500);
}

describe('Liquidity cluster detector', () => {
  it('forms a Bid Liquidity Cluster, then marks it Broken Liquidity when traded through', () => {
    const h = harness();
    for (const p of [99.5, 99.4, 99.3]) h.sim.set('bid', p, 20);
    h.idle(20_000);
    expect(h.events.filter((e) => e.kind === 'cluster')).toHaveLength(0); // not held long enough yet
    h.idle(12_000);
    const c = h.events.filter((e) => e.kind === 'cluster');
    expect(c).toHaveLength(1);
    expect(c[0]).toMatchObject({ title: 'Кластер ликвидности bid', side: 'bid', price: 99.3, priceHi: 99.6 });
    expect(c[0].confidence).toBeGreaterThan(0);
    h.step([[99.1, 1, -1, false]]);
    h.idle(1000);
    const upd = h.events.find((e) => e.id === c[0].id)!;
    expect(upd.status).toBe('broken');
    expect(upd.title).toBe('Пробитая ликвидность');
  });

  it('labels a cluster Tested Liquidity after aggressive volume hits it and it survives', () => {
    const h = harness();
    for (const p of [100.0, 99.9, 99.8]) h.sim.set('bid', p, 20);
    h.idle(32_000);
    h.step([[100.0, 2, -1]]);
    h.idle(1000);
    const cl = h.engine.clusters.list().find((x) => x.side === 'bid')!;
    expect(cl.status).toBe('tested');
    expect(cl.label).toBe('Протестированная ликвидность');
  });

  it('does not announce a second event when a zone briefly drops out of the book and returns', () => {
    const h = harness();
    for (const p of [99.5, 99.4, 99.3]) h.sim.set('bid', p, 20);
    h.idle(32_000);
    for (const p of [99.5, 99.4, 99.3]) h.sim.set('bid', p, 4);
    h.idle(5000);
    for (const p of [99.5, 99.4, 99.3]) h.sim.set('bid', p, 20);
    h.idle(35_000);
    const ids = new Set(h.events.filter((e) => e.kind === 'cluster').map((e) => e.id));
    expect(ids.size).toBe(1);
  });

  it('ignores ordinary uniform depth', () => {
    const h = harness();
    h.idle(10_000);
    expect(h.events.filter((e) => e.kind === 'cluster')).toHaveLength(0);
  });
});

describe('Absorption detector', () => {
  it('detects heavy selling absorbed at the bid without price progress', () => {
    const h = harness();
    baseline(h, 70_000);
    expect(h.events.filter((e) => e.kind === 'absorption')).toHaveLength(0);
    for (let i = 0; i < 25; i++) h.step([[100.0, 1.5, -1, false]], 200);
    const a = h.events.filter((e) => e.kind === 'absorption');
    expect(a.length).toBeGreaterThanOrEqual(1);
    expect(a[0].side).toBe('bid');
    expect(a[0].price).toBe(100);
    expect(a[0].title).toMatch(/Поглощение/);
    expect(a[0].explain).toMatch(/absorbed/);
  });

  it('does not call it absorption when price gives way', () => {
    const h = harness();
    baseline(h, 70_000);
    let p = 100.0;
    for (let i = 0; i < 25; i++) {
      h.step([[+p.toFixed(1), 1.5, -1]], 200);
      p -= 0.1;
    }
    expect(h.events.filter((e) => e.kind === 'absorption')).toHaveLength(0);
  });
});

describe('Sweep / stop-run detectors', () => {
  it('detects a buy sweep through several levels and a stop run when price reclaims the swing', () => {
    const h = harness();
    baseline(h, 120_000);
    const sweep: [number, number, 1][] = [];
    for (let k = 1; k <= 9; k++) sweep.push([+(100 + k * 0.1).toFixed(1), 2, 1]);
    h.step(sweep, 50);
    const sw = h.events.filter((e) => e.kind === 'sweep');
    expect(sw).toHaveLength(1);
    expect(sw[0]).toMatchObject({ side: 'buy', price: 100.9, priceHi: 100.9 });
    // back below the 30-bar swing high (100.5) within the reclaim window
    h.step([[100.3, 0.5, -1, false]], 500);
    const sr = h.events.filter((e) => e.kind === 'stop_run');
    expect(sr).toHaveLength(1);
    expect(sr[0]).toMatchObject({ side: 'sell', price: 100.5 });
  });

  it('small moves are not sweeps', () => {
    const h = harness();
    baseline(h, 120_000);
    h.step([[100.1, 2, 1], [100.2, 2, 1]], 50);
    expect(h.events.filter((e) => e.kind === 'sweep')).toHaveLength(0);
  });
});

describe('Imbalance / burst / divergence', () => {
  it('reports sustained order-book imbalance once', () => {
    const h = harness();
    for (let i = 0; i < 10; i++) h.sim.set('bid', +(100 - i * 0.1).toFixed(1), 50);
    h.idle(5000);
    const im = h.events.filter((e) => e.kind === 'imbalance');
    expect(im).toHaveLength(1);
    expect(im[0].side).toBe('bid');
    expect(Number(im[0].data?.obi)).toBeGreaterThan(0.6);
  });

  it('reports a volume burst against the 1s volume distribution', () => {
    const h = harness();
    baseline(h, 150_000);
    h.step([[100.1, 30, 1, false]], 1000);
    h.step([[100.1, 0.1, 1, false]], 100);
    const b = h.events.filter((e) => e.kind === 'volume_burst');
    expect(b).toHaveLength(1);
    expect(b[0].side).toBe('buy');
  });

  it('detects bearish delta divergence (new high, CVD lower high)', () => {
    const h = harness();
    const bars = Array.from({ length: 10 }, (_, i) => ({ t: i * 60_000, o: 100, h: 101, l: 99, c: 100, v: 10, bv: 5 }));
    const cvd = [0, 5, 10, 20, 30, 25, 20, 15, 10, 5];
    h.engine.flow.setBars(bars, cvd);
    h.engine.flow.onBarClose({ t: 600_000, o: 100, h: 101.5, l: 100, c: 101, v: 10, bv: 4 }, 3);
    const d = h.events.filter((e) => e.kind === 'delta_divergence');
    expect(d).toHaveLength(1);
    expect(d[0].title).toMatch(/медвежья/);
  });
});
