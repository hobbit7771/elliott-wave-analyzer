// TEST-ONLY synthetic sleeve snapshots for the portfolio risk manager.
import { SITE_PORTFOLIO_PARAMS as P, drawdownVerdict, dailyReturns, exposures, managedEquity, maxDrawdown, riskMultiplier, type Snap } from '../src/core/portfolio.js';

const DAY = 86_400_000;
const T0 = Date.UTC(2026, 0, 1, 12);
/** one snapshot a day; trend R and factor equity follow the given daily moves, carry grows 0.02 %/day */
function snaps(trendDaily: number[], factorDaily: number[]): Snap[] {
  const out: Snap[] = [{ t: T0, trendR: 0, carry: 10_000, factor: 10_000 }];
  for (let i = 0; i < trendDaily.length; i++) {
    const a = out[out.length - 1];
    out.push({ t: T0 + (i + 1) * DAY, trendR: a.trendR + trendDaily[i], carry: a.carry * 1.0002, factor: a.factor * (1 + factorDaily[i]) });
  }
  return out;
}

describe('portfolio risk manager', () => {
  it('turns sleeve snapshots into daily portfolio returns (last snapshot of each day)', () => {
    const s = snaps([2, -1], [0.01, -0.02]);
    s.splice(1, 0, { t: T0 + DAY - 3600_000, trendR: 99, carry: 1, factor: 1 }); // an earlier snapshot of day 1 is superseded by the day's last one
    const r = dailyReturns(s, P);
    expect(r).toHaveLength(2);
    expect(r[0].trend).toBeCloseTo(2 * P.trendRisk, 12);
    expect(r[0].carry).toBeCloseTo(0.0002, 12);
    expect(r[0].factor).toBeCloseTo(0.01 * P.factorWeight, 12);
    expect(r[1].trend).toBeCloseTo(-1 * P.trendRisk, 12);
  });

  it('keeps k = 1 in calm or short histories and cuts exposure when the last 30 days are more volatile than usual', () => {
    const calm = Array.from({ length: 120 }, (_, i) => (i % 2 ? 0.4 : -0.4));
    const flat = Array.from({ length: 120 }, () => 0);
    expect(riskMultiplier(dailyReturns(snaps(calm.slice(0, 20), flat.slice(0, 20)), P), P)).toBe(1); // too little history
    expect(riskMultiplier(dailyReturns(snaps(calm, flat), P), P)).toBeGreaterThan(0.97); // same regime: ≈ 1 (sampling noise only)
    const stormy = [...calm.slice(0, 90), ...Array.from({ length: 30 }, (_, i) => (i % 2 ? 3 : -3))];
    const k = riskMultiplier(dailyReturns(snaps(stormy, flat), P), P);
    expect(k).toBeGreaterThan(0.2);
    expect(k).toBeLessThan(0.6);
  });

  it('managed equity scales only the directional part by the previous day multiplier; drawdown and exposure limits', () => {
    const r = dailyReturns(snaps([4, -4], [0, 0]), P);
    const path = managedEquity(r, P);
    const exp1 = P.capital * (1 + 4 * P.trendRisk + 0.0002);
    expect(path[0].eq).toBeCloseTo(exp1, 6);
    expect(path[1].eq).toBeCloseTo(exp1 * (1 - 4 * P.trendRisk + 0.0002), 6);
    expect(maxDrawdown(path)).toBeCloseTo(1 - path[1].eq / path[0].eq, 9);
    const ex = exposures([{ symbol: 'A', sleeve: 'trend', notional: 1000 }, { symbol: 'A', sleeve: 'factor', notional: 800 }, { symbol: 'B', sleeve: 'factor', notional: -300 }], 10_000, P);
    expect(ex[0]).toMatchObject({ symbol: 'A', net: 1800, overLimit: true });
    expect(ex[1]).toMatchObject({ symbol: 'B', net: -300, overLimit: false });
  });
});

import { trackVsBacktest } from '../src/core/portfolio.js';
describe('live vs backtest tracking', () => {
  it('expected return grows with time, the band with its square root; verdicts', () => {
    const a = trackVsBacktest(0.05, 365, 0.2, 0.1);
    expect(a.expected).toBeCloseTo(0.2, 9);
    expect(a.sd).toBeCloseTo(0.1, 9);
    expect(a.z).toBeCloseTo(-1.5, 9);
    expect(a.verdict).toMatch(/ниже ожиданий/);
    expect(trackVsBacktest(-0.2, 365, 0.2, 0.1).verdict).toMatch(/проверить/);
    expect(trackVsBacktest(-0.5, 10, 0.2, 0.1).verdict).toBe('мало данных');
    expect(trackVsBacktest(0.0, 91.25, 0.2, 0.1).sd).toBeCloseTo(0.05, 9);
  });
  it('drawdown verdict against the Monte-Carlo percentiles of the backtest', () => {
    expect(drawdownVerdict(0.05)).toBe('в пределах обычного');
    expect(drawdownVerdict(0.15)).toMatch(/обычная/);
    expect(drawdownVerdict(0.2)).toMatch(/редкая/);
    expect(drawdownVerdict(0.25)).toMatch(/проверить/);
  });
});
