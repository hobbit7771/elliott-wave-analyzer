// TEST-ONLY synthetic market for the trend (Donchian breakout) model on 15m bars.
import type { Candle } from '../src/core/types.js';
import { SITE_TREND_PARAMS, TrendEngine, dailyTrend } from '../src/core/levelEngine/trend.js';

const DAY = 86_400_000;
const M15 = 15 * 60_000;
const T0 = Date.UTC(2025, 0, 1);
const meta = { symbol: 'TESTUSDT', exchange: 'test', tick: 0.01 };
const flat = (n: number, c = 100): Candle[] => Array.from({ length: n }, (_, i) => ({ t: T0 + i * DAY, o: c, h: c + 1, l: c - 1, c, v: 1, bv: 0 }));
/** one UTC day of 15m bars: flat at p except the given overrides */
function day(t: number, p: number, over: Record<number, [number, number, number, number]> = {}): Candle[] {
  return Array.from({ length: 96 }, (_, k) => {
    const [o, h, l, c] = over[k] ?? [p, p + 0.2, p - 0.2, p];
    return { t: t + k * M15, o, h, l, c, v: 1, bv: 0 };
  });
}
const up = (n: number): Candle[] => Array.from({ length: n }, (_, i) => ({ t: T0 + i * DAY, o: 100 + i, h: 101 + i, l: 99 + i, c: 100.5 + i, v: 1, bv: 0 }));
const down = (n: number): Candle[] => up(n).map((x, i, a) => ({ ...a[a.length - 1 - i], t: x.t }));

describe('trend model (Donchian breakout)', () => {
  it('buys only on the bar that trades through the 20-day high, stop 3 ATR below, then trails to the 10-day low', () => {
    const d = flat(60);
    const eng = new TrendEngine(meta, d, SITE_TREND_PARAMS, up(80)); // market factor in an uptrend
    const t = T0 + 60 * DAY;
    const bars = day(t, 100, { 10: [100, 100.9, 99.9, 100.8], 11: [100.8, 102, 100.7, 101.8] });
    const fills: number[] = [];
    bars.forEach((b, k) => eng.step(b).length && fills.push(k));
    expect(fills).toEqual([11]); // 100.9 did not reach the channel high 101
    const s = eng.setups[0];
    expect(s.direction).toBe('LONG');
    expect(s.entry).toBeCloseTo(101, 6);
    expect(s.sl).toBeCloseTo(101 - 3 * 2, 1); // ATR(20) of the flat days = 2
    // the entry day closes; the stop trails to the lowest low of the last 10 closed days (99)
    for (const b of day(t + DAY, 101.5, { 30: [101.5, 101.6, 98.9, 99.2] })) eng.step(b);
    expect(s.outcome.status).toBe('loss');
    expect(s.outcome.r).toBeGreaterThan(-0.5); // exited at the trailed 99, not at the initial 95
    expect(s.outcome.r).toBeLessThan(-0.3);
  });

  it('no long while BTC is in a D1 downtrend; no shorts at all in long-only mode', () => {
    expect(dailyTrend(down(80))).toBe(-1);
    const eng = new TrendEngine(meta, flat(60), SITE_TREND_PARAMS, down(80));
    const t = T0 + 60 * DAY;
    for (const b of day(t, 100, { 11: [100.8, 102, 100.7, 101.8], 50: [101, 101, 97, 97.5] })) eng.step(b);
    expect(eng.setups).toHaveLength(0);
  });
});
