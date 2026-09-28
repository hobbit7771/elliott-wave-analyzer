// TEST-ONLY synthetic universe for the cross-sectional funding factor.
import { SITE_FACTOR_PARAMS, accrueFactor, factorEquity, newFactorState, rebalance, targets, type FactorInput } from '../src/core/fundingFactor.js';
import type { Funding } from '../src/core/carry.js';

const DAY = 86_400_000;
const H8 = 8 * 3600_000;
const T0 = Date.UTC(2026, 0, 12) + 6 * 60_000; // 00:06 UTC
const fund = (rate: number, to: number): Funding[] => Array.from({ length: 27 }, (_, i) => ({ t: to - (27 - i) * H8, rate }));
/** 12 coins, funding rising with the index; coin 0 is twice as volatile as the rest */
const universe = (now: number, price = (i: number) => 10 + i): FactorInput[] =>
  Array.from({ length: 12 }, (_, i) => ({ symbol: `C${i}USDT`, price: price(i), settlements: fund(0.00005 * i, now), vol: i === 0 ? 0.8 : 0.4 }));

describe('funding factor (paper)', () => {
  it('longs the lowest-funding third, shorts the highest-funding third, inverse-vol weights, equal gross', () => {
    const tg = targets(universe(T0), T0, SITE_FACTOR_PARAMS)!;
    const longs = [...tg].filter(([, v]) => v > 0).map(([s]) => s);
    const shorts = [...tg].filter(([, v]) => v < 0).map(([s]) => s);
    expect(longs.sort()).toEqual(['C0USDT', 'C1USDT', 'C2USDT', 'C3USDT']);
    expect(shorts.sort()).toEqual(['C10USDT', 'C11USDT', 'C8USDT', 'C9USDT']);
    const g = SITE_FACTOR_PARAMS.gross * SITE_FACTOR_PARAMS.capital;
    expect(longs.reduce((a, s) => a + tg.get(s)!, 0)).toBeCloseTo(g, 6);
    expect(shorts.reduce((a, s) => a + tg.get(s)!, 0)).toBeCloseTo(-g, 6);
    expect(tg.get('C0USDT')!).toBeCloseTo(tg.get('C1USDT')! / 2, 6); // twice the vol → half the size
    expect(targets(universe(T0).slice(0, 5), T0, SITE_FACTOR_PARAMS)).toBeNull(); // too few coins
  });

  it('rebalances weekly; equity changes only by fees at the trade, then by price and funding', () => {
    const st = newFactorState(T0);
    const u = universe(T0);
    expect(rebalance(st, u, T0)).toBe(true);
    const fees = st.positions.reduce((a, x) => a + x.fees, 0);
    const px = (s: string) => u.find((x) => x.symbol === s)!.price;
    expect(factorEquity(st, px)).toBeCloseTo(SITE_FACTOR_PARAMS.capital - fees, 6);
    expect(rebalance(st, u, T0 + 3 * DAY)).toBe(false); // not a week yet
    // funding: one more settlement at +0.01 %: longs pay, shorts receive
    const later = T0 + DAY;
    const next = u.map((x) => ({ ...x, settlements: [...x.settlements, { t: T0 + H8, rate: 0.0001 }] }));
    const before = st.cash;
    accrueFactor(st, new Map(next.map((x) => [x.symbol, x])), later);
    const exp = st.positions.reduce((a, x) => a - x.qty * px(x.symbol) * 0.0001, 0);
    expect(st.cash - before).toBeCloseTo(exp, 9);
    expect(Math.abs(exp)).toBeLessThan(1e-6); // equal gross long and short → the flat rate nets out
    // a 10 % rally of every coin barely moves a market-neutral book
    const up = (s: string) => px(s) * 1.1;
    expect(Math.abs(factorEquity(st, up) - factorEquity(st, px))).toBeLessThan(1e-6);
    expect(rebalance(st, universe(T0 + 7 * DAY), T0 + 7 * DAY)).toBe(true); // a week later, with fresh history
  });
});
