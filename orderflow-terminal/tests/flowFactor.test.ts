// TEST-ONLY synthetic daily bars for the order-flow factor flow7.
import { SITE_FLOW_PARAMS, flowFeatures, flowTargets, marketBetas, type FlowInput } from '../src/core/flowFactor.js';
import { applyTargets, factorEquity, newFactorState } from '../src/core/fundingFactor.js';
import type { Candle } from '../src/core/types.js';

const DAY = 86_400_000;
const bars = (n: number, buyShare: (i: number) => number, price = (i: number) => 100 * (1 + 0.01 * Math.sin(i))): Candle[] =>
  Array.from({ length: n }, (_, i) => ({ t: i * DAY, o: price(i), h: price(i) * 1.01, l: price(i) * 0.99, c: price(i), v: 1000, bv: 1000 * buyShare(i) }));

describe('flow7 order-flow factor (paper forward test)', () => {
  it('flow7 is the 7-day mean of (buy − sell) / volume; NaN on short history', () => {
    const k = bars(40, (i) => (i >= 33 ? 0.6 : 0.5));
    expect(flowFeatures(k).flow7).toBeCloseTo(0.2, 9); // last 7 bars: (2·600 − 1000) / 1000
    expect(flowFeatures(bars(40, () => 0.45)).flow7).toBeCloseTo(-0.1, 9);
    expect(flowFeatures(k).vol).toBeGreaterThan(0);
    expect(flowFeatures(k).qv30).toBeGreaterThan(0);
    expect(Number.isNaN(flowFeatures(bars(20, () => 0.6)).flow7)).toBe(true);
  });

  it('longs the strongest buying pressure, shorts the strongest selling, top-50 by volume, equal gross', () => {
    const p = SITE_FLOW_PARAMS;
    const inputs: FlowInput[] = Array.from({ length: 60 }, (_, i) => ({ symbol: `C${i}USDT`, price: 10, flow7: (i - 30) / 100, vol: i === 45 ? 1.2 : 0.6, qv30: i < 50 ? 1e9 - i : 1 }));
    const tg = flowTargets(inputs, p)!;
    const longs = [...tg].filter(([, v]) => v > 0).map(([s]) => s);
    const shorts = [...tg].filter(([, v]) => v < 0).map(([s]) => s);
    expect(longs.length).toBe(10);
    expect(shorts.length).toBe(10);
    expect(longs).toContain('C49USDT'); // highest flow7 inside the volume universe
    expect(longs).not.toContain('C59USDT'); // outside the universe (low volume)
    expect(shorts).toContain('C0USDT');
    const g = p.gross * p.capital;
    expect(longs.reduce((a, s) => a + tg.get(s)!, 0)).toBeCloseTo(g, 6);
    expect(shorts.reduce((a, s) => a + tg.get(s)!, 0)).toBeCloseTo(-g, 6);
    expect(tg.get('C45USDT')!).toBeCloseTo(tg.get('C46USDT')! / 2, 6); // twice the vol → half the size
    expect(flowTargets(inputs.slice(0, 20), p)).toBeNull(); // too few coins
  });

  it('beta-neutral: scales the short side so both sides carry the same market beta', () => {
    const p = SITE_FLOW_PARAMS;
    const inputs: FlowInput[] = Array.from({ length: 40 }, (_, i) => ({ symbol: `C${i}USDT`, price: 10, flow7: i / 100, vol: 0.6, qv30: 1e9, beta: i < 20 ? 2 : 1 }));
    const tg = flowTargets(inputs, p)!;
    const sum = (sign: number) => [...tg].filter(([, v]) => Math.sign(v) === sign).reduce((a, [, v]) => a + v, 0);
    expect(sum(1)).toBeCloseTo(p.gross * p.capital, 6);
    expect(sum(-1)).toBeCloseTo(-p.gross * p.capital / 2, 6); // shorts have beta 2 → half the gross
    expect(flowTargets(inputs, { ...p, betaNeutral: false })!.get('C0USDT')).toBeCloseTo(-p.gross * p.capital / 10, 6);
  });

  it('market betas: a coin that moves twice the market has beta ≈ 2', () => {
    const mk = (f: number) => Array.from({ length: 70 }, (_, i) => ({ t: i * DAY, o: 1, h: 1, l: 1, c: Math.exp(f * 0.02 * Math.sin(i * 1.3) + (f === 2 ? 0.001 * ((i * 7) % 3) : 0)), v: 1, bv: 0.5 }));
    const b = marketBetas(new Map([['A', mk(1)], ['B', mk(1)], ['C', mk(1)], ['D', mk(1)], ['E', mk(2)]]));
    expect(b.get('A')!).toBeLessThan(b.get('E')!);
    expect(b.get('E')! / b.get('A')!).toBeGreaterThan(1.7);
  });

  it('applies targets with fees only on the traded notional', () => {
    const st = newFactorState(0, SITE_FLOW_PARAMS);
    const tg = new Map([['AUSDT', 5000], ['BUSDT', -5000]]);
    const px = new Map([['AUSDT', 10], ['BUSDT', 20]]);
    applyTargets(st, tg, px, 6 * 60_000);
    expect(factorEquity(st, (s) => px.get(s))).toBeCloseTo(10_000 - 10_000 * SITE_FLOW_PARAMS.feePerSide, 6);
    applyTargets(st, tg, px, DAY * 7 + 6 * 60_000); // same targets: no trade, no fee
    expect(factorEquity(st, (s) => px.get(s))).toBeCloseTo(10_000 - 10_000 * SITE_FLOW_PARAMS.feePerSide, 6);
  });
});
