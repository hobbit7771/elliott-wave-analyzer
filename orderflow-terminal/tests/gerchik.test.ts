// TEST-ONLY synthetic market for the Gerchik engine: daily candles with a defended support near 100, then 15m
// bars that touch the level (БПУ1), come back to it (БПУ2 fill) and run to the target or the stop.
import type { Candle } from '../src/core/types.js';
import { DEFAULT_GERCHIK_PARAMS, GerchikEngine, type GerchikParams } from '../src/core/levelEngine/gerchik.js';

const DAY = 86_400_000;
const M15 = 15 * 60_000;
const T0 = Date.UTC(2025, 0, 1);
const meta = { symbol: 'TESTUSDT', exchange: 'test', tick: 0.01 };

function daily(tail: number[] = [102, 104, 106, 108, 110]): Candle[] {
  const path: number[] = [];
  for (let k = 0; k < 4; k++) {
    for (let i = 0; i <= 10; i++) path.push(101 + (18 * i) / 10);
    for (let i = 0; i <= 10; i++) path.push(119 - (18 * i) / 10);
  }
  path.push(...tail);
  let prev = path[0];
  return path.map((c, i) => {
    const o = prev;
    prev = c;
    return { t: T0 + i * DAY, o, h: Math.max(o, c) + (c >= 118.5 ? 2 : 0.8), l: Math.min(o, c) - (c <= 101.5 ? 2 : 0.8), c, v: c <= 102 || c >= 118 ? 3000 : 1000, bv: 0 };
  });
}

const P: GerchikParams = { ...DEFAULT_GERCHIK_PARAMS, minStrength: 0, trend: 'none', roomCheck: false, atrExhaust: Infinity, falseBreak: false, breakout: false, bpuWindow: 48 };

function setup(p: GerchikParams, d = daily()) {
  const eng = new GerchikEngine(meta, d, p);
  const lv = eng.activeLevels().find((l) => Math.abs(l.price - 100) < 3)!;
  const S = p.stopAtr * eng.atrDaily;
  const L = p.luftFrac * S;
  let t = d[d.length - 1].t + DAY;
  const bar = (o: number, h: number, l: number, c: number) => {
    const b = { t, o, h, l, c, v: 100, bv: 0 };
    t += M15;
    return b;
  };
  return { eng, lv, S, L, bar };
}

describe('Gerchik engine', () => {
  it('places the БПУ1 limit on a closed bar and fills it only on a later bar (no look-ahead), at level + luft', () => {
    const { eng, lv, S, L, bar } = setup(P);
    const x = lv.price;
    expect(eng.step(bar(x + 3 * S, x + 3 * S, x + 2 * S, x + 2 * S))).toEqual([]); // approach from above
    // БПУ1: low exactly at the level, close back above it → an order, no trade yet
    expect(eng.step(bar(x + 2 * S, x + 2 * S, x, x + 0.8 * S))).toEqual([]);
    expect(eng.stateOf(lv.id)).toBe('TOUCH');
    // БПУ2: price returns to level + luft → filled
    const filled = eng.step(bar(x + 0.8 * S, x + S, x + 0.1 * L, x + 0.5 * S));
    expect(filled).toHaveLength(1);
    const s = filled[0];
    expect(s.direction).toBe('LONG');
    expect(s.entry).toBeCloseTo(x + L, 6);
    expect(s.sl).toBeCloseTo(x + L - S, 6);
    expect(s.tp).toBeCloseTo(x + L + 3 * S, 6);
    expect(s.trigger).toMatch(/Отбой/);
    // the target is reached later: a win worth 3R minus Bybit fees
    eng.step(bar(x + 0.5 * S, x + 4 * S, x + 0.5 * S, x + 3.9 * S));
    expect(s.outcome.status).toBe('win');
    expect(s.outcome.r).toBeLessThan(3);
    expect(s.outcome.r).toBeGreaterThan(2.8);
  });

  it('counts the stop first when one bar reaches both the stop and the target', () => {
    const { eng, lv, S, bar } = setup(P);
    const x = lv.price;
    eng.step(bar(x + 3 * S, x + 3 * S, x + 2 * S, x + 2 * S));
    eng.step(bar(x + 2 * S, x + 2 * S, x, x + 0.8 * S));
    const [s] = eng.step(bar(x + 0.8 * S, x + S, x + 0.1 * S, x + 0.5 * S));
    eng.step(bar(x + 0.5 * S, x + 5 * S, x - 2 * S, x)); // wide bar through both
    expect(s.outcome.status).toBe('loss');
    expect(s.outcome.r).toBeLessThan(-1); // −1R and the fees
  });

  it('does not buy a support against a D1 downtrend with the strict trend filter', () => {
    const down = daily([118, 116, 114, 112, 110, 108, 107, 106, 105, 104, 103.5, 103]);
    const { eng, lv, S, bar } = setup({ ...P, trend: 'strict', trendSma: 20 }, down);
    const x = lv.price;
    eng.step(bar(x + 3 * S, x + 3 * S, x + 2 * S, x + 2 * S));
    eng.step(bar(x + 2 * S, x + 2 * S, x, x + 0.8 * S));
    expect(eng.step(bar(x + 0.8 * S, x + S, x + 0.1 * S, x + 0.5 * S))).toEqual([]);
    expect(eng.setups).toHaveLength(0);
  });

  it('re-reads the side of the level on every bar: a touch from below makes it resistance (SHORT), not a long', () => {
    const { eng, lv, S, L, bar } = setup(P);
    const x = lv.price;
    eng.step(bar(x - 3 * S, x - 2 * S, x - 3 * S, x - 2 * S)); // price below the level
    eng.step(bar(x - 2 * S, x, x - 2 * S, x - 0.8 * S)); // БПУ1 from below
    const [s] = eng.step(bar(x - 0.8 * S, x - 0.1 * L, x - S, x - 0.5 * S));
    expect(s.direction).toBe('SHORT');
    expect(s.entry).toBeCloseTo(x - L, 6);
  });
});
