// TEST-ONLY synthetic daily candles.
import { staticLevels } from '../src/core/staticLevels.js';
import type { Candle } from '../src/core/types.js';

/** price oscillates between ~100 (support) and ~120 (resistance) several times, then breaks above 120 */
function series(): Candle[] {
  const out: Candle[] = [];
  let t = Date.UTC(2025, 0, 1);
  const path: number[] = [];
  for (let k = 0; k < 4; k++) {
    for (let i = 0; i <= 10; i++) path.push(101 + (18 * i) / 10); // up to ~119
    for (let i = 0; i <= 10; i++) path.push(119 - (18 * i) / 10); // down to ~101
  }
  for (let i = 0; i < 20; i++) path.push(121 + i); // breakout
  let prev = path[0];
  for (const p of path) {
    const o = prev;
    const c = p;
    // wicks poke through the extremes and get rejected
    const h = Math.max(o, c) + (c >= 118.5 ? 2 : 0.8);
    const l = Math.min(o, c) - (c <= 101.5 ? 2 : 0.8);
    out.push({ t, o, h, l, c, v: c <= 102 || c >= 118 ? 3000 : 1000, bv: 0 });
    t += 86_400_000;
    prev = p;
  }
  return out;
}

describe('Static daily levels', () => {
  it('finds the repeatedly defended support and resistance, at most 4, with honest explanations', () => {
    const lv = staticLevels(series(), { max: 4 });
    expect(lv.length).toBeGreaterThanOrEqual(2);
    expect(lv.length).toBeLessThanOrEqual(4);
    const near = (p: number) => lv.find((l) => Math.abs(l.price - p) <= 2.5);
    expect(near(100)).toBeTruthy();
    expect(near(120)).toBeTruthy();
    expect(near(100)!.rejections).toBeGreaterThanOrEqual(2);
    expect(near(100)!.why).toMatch(/оценка по дневным OHLCV/);
  });

  it('needs enough history', () => {
    expect(staticLevels(series().slice(0, 20))).toEqual([]);
  });
});
