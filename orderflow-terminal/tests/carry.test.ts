// TEST-ONLY synthetic quotes for the paper funding-carry strategy.
import { SITE_CARRY_PARAMS, accrue, decide, newCarryState, slotNotional, totals, trailingApr, type CarryQuote, type Funding } from '../src/core/carry.js';

const DAY = 86_400_000;
const T0 = Date.UTC(2026, 0, 10);
const H8 = 8 * 3600_000;
/** 8-hourly settlements of a constant rate over [from, to) */
const fund = (rate: number, from: number, to: number): Funding[] => {
  const out: Funding[] = [];
  for (let t = from; t < to; t += H8) out.push({ t, rate });
  return out;
};
const quote = (symbol: string, rate: number, now: number, spot = 100, perp = 100.05): CarryQuote => ({ symbol, spot, perp, funding: rate, nextFundingTime: now + H8, turnover: 1e9, settlements: fund(rate, now - 9 * DAY, now) });

describe('funding carry (paper)', () => {
  it('trailing APR = mean daily funding × 365, NaN without enough settlements', () => {
    const now = T0 + 5 * 60_000;
    expect(trailingApr(fund(0.0001, T0 - 7 * DAY, T0 + 1), now, 7)).toBeCloseTo(0.0003 * 365, 9); // 0.01 % / 8 h ≈ 10.95 %/yr
    expect(trailingApr(fund(0.0001, T0 - 2 * DAY, T0), now, 7)).toBeNaN();
  });

  it('opens the highest-funding coins above the entry threshold, once a day after 00:05 UTC', () => {
    const st = newCarryState(T0);
    const early = T0 + 60_000; // 00:01 — too early
    const q = new Map([
      ['AAAUSDT', quote('AAAUSDT', 0.0003, early)], // ≈ 33 %/yr
      ['BBBUSDT', quote('BBBUSDT', 0.00005, early)], // ≈ 5.5 %/yr: below 10 %
      ['CCCUSDT', quote('CCCUSDT', 0.0002, early)], // ≈ 22 %/yr
    ]);
    expect(decide(st, q, early)).toBe(false);
    const now = T0 + 6 * 60_000;
    expect(decide(st, q, now)).toBe(true);
    expect(st.positions.map((p) => p.symbol)).toEqual(['AAAUSDT', 'CCCUSDT']);
    expect(st.positions[0].notional).toBeCloseTo(slotNotional(SITE_CARRY_PARAMS), 9);
    expect(decide(st, q, now + 3600_000)).toBe(false); // once per UTC day
  });

  it('accrues each later settlement once, pays fees both ways, closes when funding fades', () => {
    const st = newCarryState(T0);
    const now = T0 + 6 * 60_000;
    const q = new Map([['AAAUSDT', quote('AAAUSDT', 0.0003, now)]]);
    decide(st, q, now);
    const pos = st.positions[0];
    const N = pos.notional;
    // three settlements the next day at 0.03 % each
    const later = T0 + DAY + 6 * 60_000;
    q.get('AAAUSDT')!.settlements.push(...fund(0.0003, T0 + H8, T0 + DAY + 1));
    accrue(st, q, later);
    accrue(st, q, later); // idempotent
    expect(pos.funding).toBeCloseTo(3 * 0.0003 * N, 9);
    // funding turns to zero for a week: trailing APR ≤ 3 % → the position is closed at the next decision
    const week = T0 + 8 * DAY + 6 * 60_000;
    const cold = quote('AAAUSDT', 0, week, 101, 101.05); // price moved, spot and perp moved together
    cold.settlements = [...q.get('AAAUSDT')!.settlements.filter((s) => s.t <= T0 + DAY), ...fund(0, T0 + DAY + H8, week)];
    decide(st, new Map([['AAAUSDT', cold]]), week);
    expect(st.positions).toHaveLength(0);
    const c = st.closed[0];
    expect(c.reason).toMatch(/фандинг упал/);
    expect(c.fees).toBeCloseTo(2 * N * SITE_CARRY_PARAMS.feePerSide, 9);
    expect(Math.abs(c.basis)).toBeLessThan(N * 0.001); // market-neutral: the 1 % price move nearly cancels
    expect(c.net).toBeCloseTo(c.funding + c.basis - c.fees, 9);
    expect(totals(st, new Map()).equity).toBeCloseTo(SITE_CARRY_PARAMS.capital + c.net, 9);
  });
});
