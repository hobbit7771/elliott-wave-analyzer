// TEST-ONLY synthetic market: daily candles that make a support at ~100 and a resistance at ~120, then
// 15m bars that approach the support calmly, sweep it, and react with a volume spike.
import type { Candle } from '../src/core/types.js';
import { analyzeDailyLevels, DEFAULT_DAILY_LEVEL_PARAMS } from '../src/core/levelEngine/dailyLevels.js';
import { DEFAULT_SETUP_PARAMS, LevelSetupEngine } from '../src/core/levelEngine/setupEngine.js';
import { runHistory, stats, walkForward } from '../src/core/levelEngine/backtest.js';

const DAY = 86_400_000;
const M15 = 15 * 60_000;
const T0 = Date.UTC(2025, 0, 1);

function dailyRange(cycles = 4, endAt = 110): Candle[] {
  const path: number[] = [];
  for (let k = 0; k < cycles; k++) {
    for (let i = 0; i <= 10; i++) path.push(101 + (18 * i) / 10);
    for (let i = 0; i <= 10; i++) path.push(119 - (18 * i) / 10);
  }
  for (let i = 1; i <= 5; i++) path.push(101 + ((endAt - 101) * i) / 5);
  const out: Candle[] = [];
  let prev = path[0];
  path.forEach((p, i) => {
    const o = prev;
    const c = p;
    const h = Math.max(o, c) + (c >= 118.5 ? 2 : 0.8);
    const l = Math.min(o, c) - (c <= 101.5 ? 2 : 0.8);
    out.push({ t: T0 + i * DAY, o, h, l, c, v: c <= 102 || c >= 118 ? 3000 : 1000, bv: 0 });
    prev = p;
  });
  return out;
}

/** 15m bars: calm drift down to the support with decaying volume and ranges, sweep, spike reaction, rally. */
function approachBars(start: number): Candle[] {
  const bars: Candle[] = [];
  let t = start;
  let px = 110;
  const push = (o: number, c: number, range: number, v: number) => {
    bars.push({ t, o, h: Math.max(o, c) + range, l: Math.min(o, c) - range, c, v, bv: 0 });
    t += M15;
    px = c;
  };
  // day 1: normal activity drifting from 110 to ~103.6
  for (let i = 0; i < 96; i++) push(px, px - 0.067, 0.35, 100);
  // approach: 12 hours from ~103.6 to ~100.9, volume and ranges contracting
  for (let i = 0; i < 48; i++) push(px, px - 0.056, 0.08, 35);
  // touch / sweep below the zone, close back near the level
  push(px, 99.9, 0.1, 45);
  bars[bars.length - 1].l = 98.2;
  // reaction: strong bullish bar with a volume spike, closes above the zone and above the recent highs
  push(99.9, 101.2, 0.1, 400);
  // rally toward the resistance
  for (let i = 0; i < 200; i++) push(px, px + 0.1, 0.1, 120);
  return bars;
}

describe('D1 level analysis', () => {
  it('scores the defended support and resistance and keeps up to 10 levels', () => {
    const lv = analyzeDailyLevels(dailyRange());
    expect(lv.length).toBeGreaterThanOrEqual(2);
    expect(lv.length).toBeLessThanOrEqual(10);
    const sup = lv.find((l) => Math.abs(l.price - 100) < 2.5)!;
    expect(sup.role).toBe('support');
    expect(sup.strength).toBeGreaterThan(0);
    expect(sup.breakdown).toHaveProperty('reaction');
  });

  it('penalises a level that price saws back and forth through (CHOP / SAW) and can mark it CHOPPED', () => {
    const d = dailyRange(4, 110);
    const before = analyzeDailyLevels(d).find((l) => Math.abs(l.price - 100) < 2.5)!;
    let t = d[d.length - 1].t;
    let o = 110;
    let seed = 7;
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
    for (const c of [108, 105, 102.5]) {
      t += DAY;
      d.push({ t, o, h: o + 0.5, l: c - 0.5, c, v: 1000, bv: 0 });
      o = c;
    }
    // 45 days: closes alternate above/below ~100 with varying amplitude, small displacement, frequent retests
    for (let i = 0; i < 45; i++) {
      t += DAY;
      const c = 100 + (i % 2 ? 1 : -1) * (0.3 + 0.9 * rnd());
      d.push({ t, o, h: Math.max(o, c) + 0.2 + 0.8 * rnd(), l: Math.min(o, c) - 0.2 - 0.8 * rnd(), c, v: 1000, bv: 0 });
      o = c;
    }
    const after = analyzeDailyLevels(d).find((l) => Math.abs(l.price - 100) < 2.5)!;
    expect(after.chopScore).toBeGreaterThan(before.chopScore + 0.2);
    expect(after.strength).toBeLessThan(before.strength);
    expect(after.why).toMatch(/пила/);
    const strict = analyzeDailyLevels(d, { ...DEFAULT_DAILY_LEVEL_PARAMS, chopThreshold: 0.3 }).find((l) => Math.abs(l.price - 100) < 2.5)!;
    expect(strict.status).toBe('CHOPPED');
    expect(strict.strength).toBeLessThanOrEqual(40);
  });

  it('distinguishes a true break (acceptance + follow-through) and a later flip', () => {
    const d = dailyRange(4, 115);
    let t = d[d.length - 1].t;
    let px = 115;
    const add = (c: number) => {
      t += DAY;
      d.push({ t, o: px, h: Math.max(px, c) + 0.8, l: Math.min(px, c) - 0.8, c, v: 1500, bv: 0 });
      px = c;
    };
    for (const c of [118, 123, 126, 129, 131, 133]) add(c); // accepted above ~120 with follow-through
    const broken = analyzeDailyLevels(d).find((l) => Math.abs(l.price - 120) < 2.5);
    expect(broken?.status).toBe('BROKEN');
    for (const c of [127, 122.6, 125, 128, 131]) add(c); // retest from above holds → flipped to support
    const flipped = analyzeDailyLevels(d).find((l) => Math.abs(l.price - 120) < 2.5);
    expect(flipped?.status).toBe('FLIPPED');
  });
});

describe('Level setup engine (one engine for history / replay / live)', () => {
  const daily = dailyRange();
  const start = daily[daily.length - 1].t + DAY;
  const bars = approachBars(start);
  const meta = { symbol: 'TESTUSDT', exchange: 'test', tick: 0.1 };
  const p = { ...DEFAULT_SETUP_PARAMS, minStrength: 0 };

  it('walks WATCHING → APPROACHING → COMPRESSION → TOUCH → REJECTION → CONFIRMED and emits one LONG', () => {
    const eng = new LevelSetupEngine(meta, daily, p);
    const states = new Set<string>();
    const sup = eng.activeLevels().find((l) => Math.abs(l.price - 100) < 2.5)!;
    expect(sup).toBeTruthy();
    for (const b of bars) {
      eng.step(b);
      states.add(eng.stateOf(sup.id));
    }
    for (const s of ['APPROACHING', 'COMPRESSION', 'TOUCH']) expect(states.has(s)).toBe(true);
    const longs = eng.setups.filter((s) => s.direction === 'LONG');
    expect(longs).toHaveLength(1);
    const s = longs[0];
    expect(s.sweep).toBe(true);
    expect(s.volumeDecay).toBeLessThan(-0.3);
    expect(s.volatilityContraction).toBeLessThan(0);
    expect(s.reactionVolumeRatio).toBeGreaterThan(1.8);
    expect(s.entry).toBeCloseTo(101.2, 5);
    expect(s.sl).toBeLessThan(98.2);
    expect(s.reasons).toEqual(expect.arrayContaining(['D1_LEVEL', 'VOL_DECAY', 'SWEEP_RECLAIM', 'REACTION_VOLUME', 'MICRO_BOS']));
    expect(s.setupQuality).toBeGreaterThan(0);
    expect(s.outcome.status).toBe('win');
  });

  it('has no look-ahead: setups up to T are identical whether or not later bars exist', () => {
    const full = runHistory(meta, daily, bars, p).setups;
    const cut = bars[bars.length - 150].t;
    const part = runHistory(meta, daily, bars.filter((b) => b.t <= cut), p).setups;
    const strip = (xs: typeof full) => xs.filter((s) => s.confirmedAt <= cut).map(({ outcome: _o, ...rest }) => rest);
    expect(strip(part)).toEqual(strip(full));
    expect(strip(full).length).toBeGreaterThan(0);
  });

  it('uses only CLOSED daily candles: the current day is not part of the level input', () => {
    const eng = new LevelSetupEngine(meta, daily, p);
    for (const b of bars.slice(0, 50)) eng.step(b); // still inside the first day
    expect(eng.daily[eng.daily.length - 1].t).toBeLessThan(bars[0].t);
    for (const b of bars.slice(50, 100)) eng.step(b); // crossed into the next day
    expect(eng.daily[eng.daily.length - 1].t).toBe(bars[0].t);
  });

  it('gives no entry without the volume-decay / contraction phase (no single-feature signals)', () => {
    const loud = bars.map((b, i) => (i >= 96 && i < 144 ? { ...b, v: 300, h: b.h + 0.4, l: b.l - 0.4 } : b));
    expect(runHistory(meta, daily, loud, p).setups.filter((s) => s.direction === 'LONG')).toHaveLength(0);
  });

  it('walk-forward reports TRAIN / VALIDATION / OOS separately and never tunes on the future', () => {
    const wf = walkForward(meta, daily, bars, p, 1);
    expect(wf.segments.map((s) => s.name)).toEqual(['TRAIN', 'VALIDATION', 'OOS']);
    expect(wf.grid.length).toBe(18);
    const st = stats(wf.setups);
    expect(st.total).toBe(wf.setups.length);
    for (const s of wf.setups) expect(['TRAIN', 'VALIDATION', 'OOS']).toContain(s.segment);
  });
});
