import { parseNdjson, replay, type ReplayRecord } from '../src/core/replay.js';
import { SimExchange, TEST_META, flatCandles } from './fixtures/sim.js';

/** Build a recording (same format as `npm run record`) of an iceberg session on the simulator. */
function recording(): ReplayRecord[] {
  const sim = new SimExchange(0.1);
  sim.seed(100);
  const out: ReplayRecord[] = [{ k: 'meta', meta: TEST_META, syncMode: 'futures', heatStep: 0.1 }, { k: 'klines', c: flatCandles(60, 100, 1, sim.t) }];
  const d0 = sim.diff();
  out.push({ k: 'diff', d: { ...d0, firstId: sim.lastId, lastId: sim.lastId, prevLastId: sim.lastId - 1 } });
  out.push({ k: 'snap', s: sim.snapshot() });
  sim.set('bid', 100, 3);
  for (let i = 0; i < 80; i++) {
    out.push({ k: 'trade', tr: sim.trade(100, 1, -1, false) }); // executed without visible depletion
    sim.advance(200);
    out.push({ k: 'diff', d: sim.diff() });
    out.push({ k: 'tick', t: sim.t });
  }
  return out;
}

describe('Replay of recorded data', () => {
  it('is deterministic: the same recording always yields identical events and heatmap', () => {
    const rec = recording();
    const text = rec.map((r) => JSON.stringify(r)).join('\n');
    const a = replay(parseNdjson(text));
    const b = replay(parseNdjson(text));
    expect(a.events.length).toBeGreaterThan(0);
    expect(JSON.stringify(a.events)).toBe(JSON.stringify(b.events));
    expect(JSON.stringify(a.columns)).toBe(JSON.stringify(b.columns));
    expect(a.trades).toBe(80);
    expect(a.gaps).toBe(0);
    expect(a.events.some((e) => e.kind === 'iceberg' && e.price === 100)).toBe(true);
    expect(a.columns.length).toBeGreaterThanOrEqual(15);
  });

  it('detects a gap in a recording with missing diffs', () => {
    const rec = recording();
    const drop = rec.findIndex((r, i) => r.k === 'diff' && i > 40);
    const withGap = rec.filter((_, i) => i !== drop);
    const r = replay(withGap);
    expect(r.gaps).toBe(1);
  });

  it('refuses recordings without meta', () => {
    expect(() => replay([{ k: 'tick', t: 1 }])).toThrow(/meta/);
  });
});
