// TEST-ONLY synthetic markets for the Street Smarts setups (Raschke & Connors) on 15m bars.
import type { Candle } from '../src/core/types.js';
import { BOOK_RASCHKE_PARAMS, RaschkeEngine, ema, lbrRsi, type RaschkeParams } from '../src/core/levelEngine/raschke.js';

const DAY = 86_400_000;
const M15 = 15 * 60_000;
const T0 = Date.UTC(2025, 0, 1);
const meta = { symbol: 'TESTUSDT', exchange: 'test', tick: 0.01 };
const only = (s: RaschkeParams['setups'][number], extra: Partial<RaschkeParams> = {}): RaschkeParams => ({ ...BOOK_RASCHKE_PARAMS, setups: [s], ...extra });

/** Daily candles around a flat price with a given low on one day. */
function flatDays(n: number, base = 100, range = 2): Candle[] {
  return Array.from({ length: n }, (_, i) => ({ t: T0 + i * DAY, o: base, h: base + range / 2, l: base - range / 2, c: base, v: 1000, bv: 0 }));
}
/** 96 15m bars of one UTC day following a path of (o, h, l, c) per bar. */
function dayBars(dayT: number, path: [number, number, number, number][]): Candle[] {
  return path.map(([o, h, l, c], k) => ({ t: dayT + k * M15, o, h, l, c, v: 10, bv: 0 }));
}

describe('LBR/RSI (3-period RSI of the 1-day change)', () => {
  it('is low when the daily drops accelerate and high when the rises accelerate', () => {
    const down = [100, 100, 99.5, 98.5, 97, 95, 92.5, 89.5, 86];
    const up = down.map((x) => 200 - x);
    expect(lbrRsi(down)).toBeLessThan(30);
    expect(lbrRsi(up)).toBeGreaterThan(70);
  });
});

describe('Raschke engine', () => {
  it('Turtle Soup: a stop order back above the prior 20-day low, filled only after the pierce bar, stop under the day low', () => {
    const d = flatDays(30);
    d[20] = { ...d[20], l: 95 }; // the prior 20-day low, 10 sessions before "today"
    const eng = new RaschkeEngine(meta, d, only('TURTLE_SOUP'));
    const day = T0 + 30 * DAY;
    const path: [number, number, number, number][] = [];
    for (let k = 0; k < 96; k++) path.push([99, 99.5, 98.5, 99]);
    path[10] = [99, 99, 94, 94.5]; // pierces 95
    path[11] = [94.5, 94.8, 94.2, 94.6]; // still below: the order (≈ 95.0x) is not reached
    path[12] = [94.6, 96, 94.4, 95.8]; // back through → filled
    const bars = dayBars(day, path);
    const filled: string[] = [];
    bars.forEach((b, k) => {
      const s = eng.step(b);
      if (s.length) filled.push(`${k}:${s[0].direction}`);
    });
    expect(filled[0]).toBe('12:LONG');
    const s = eng.setups[0];
    expect(s.entry).toBeGreaterThan(95);
    expect(s.entry).toBeLessThan(95.2);
    expect(s.sl).toBeLessThan(94); // under the day's low
    expect(s.trigger).toMatch(/Turtle Soup/);
  });

  it('ID/NR4: the first side to trigger cancels the other side', () => {
    const d = flatDays(30);
    d[26] = { ...d[26], h: 104, l: 96 };
    d[27] = { ...d[27], h: 103, l: 97 };
    d[28] = { ...d[28], h: 102, l: 98 };
    d[29] = { ...d[29], h: 101, l: 99.2 }; // inside day and the narrowest of the last 4
    const eng = new RaschkeEngine(meta, d, only('ID_NR4', { exitIfNotProfitableAtClose: false }));
    const day = T0 + 30 * DAY;
    const path: [number, number, number, number][] = [];
    for (let k = 0; k < 96; k++) path.push([100, 100.4, 99.6, 100]);
    path[5] = [100, 101.5, 99.9, 101.3]; // up through the high → LONG
    path[40] = [100.2, 100.3, 98.5, 98.7]; // later down through the low: the short side was cancelled
    for (const b of dayBars(day, path)) eng.step(b);
    expect(eng.setups).toHaveLength(1);
    expect(eng.setups[0].direction).toBe('LONG');
    expect(eng.setups[0].outcome.status).toBe('loss'); // the long's stop (the ID/NR4 low) was hit
  });

  it('Holy Grail (D1): first pullback to the 20 EMA with ADX > 30 → buy stop above the pullback day, target the prior high', () => {
    const d: Candle[] = [];
    for (let i = 0; i < 80; i++) {
      const c = 100 + i * 2;
      d.push({ t: T0 + i * DAY, o: c - 1.5, h: c + 0.5, l: c - 2, c, v: 1000, bv: 0 });
    }
    const e = ema(d.map((x) => x.c), 20);
    const last = d[d.length - 1];
    const pull = { t: T0 + 80 * DAY, o: last.c, h: last.c + 0.2, l: e[e.length - 1] - 2, c: last.c - 6, v: 1500, bv: 0 };
    const eng = new RaschkeEngine(meta, d, only('HOLY_GRAIL'));
    // the pullback day itself, as 15m bars, then the next day trades above its high
    const path1: [number, number, number, number][] = [];
    for (let k = 0; k < 96; k++) {
      const px = pull.o + ((pull.c - pull.o) * k) / 95;
      path1.push([px, Math.min(pull.h, px + 0.1), k === 60 ? pull.l : px - 0.1, px]);
    }
    const pullBars = dayBars(pull.t, path1);
    for (const b of pullBars) eng.step(b);
    expect(eng.setups).toHaveLength(0);
    const pullHigh = Math.max(...pullBars.map((b) => b.h));
    const path2: [number, number, number, number][] = [];
    for (let k = 0; k < 96; k++) path2.push([pull.c + k * 0.2, pull.c + k * 0.2 + 0.3, pull.c + k * 0.2 - 0.3, pull.c + k * 0.2 + 0.2]);
    for (const b of dayBars(pull.t + DAY, path2)) eng.step(b);
    const s = eng.setups[0];
    expect(s?.direction).toBe('LONG');
    expect(s.entry).toBeGreaterThan(pullHigh); // above the high of the bar (day) that touched the EMA
    expect(s.tp).toBeGreaterThanOrEqual(last.h); // the prior swing high
    expect(s.trigger).toMatch(/Holy Grail/);
  });
});
