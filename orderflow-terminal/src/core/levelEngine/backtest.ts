// Historical evaluation of level setups with the SAME engine used live (LevelSetupEngine.step, bar by bar).
// Walk-forward: parameters are chosen on TRAIN only, checked on VALIDATION, and reported on OUT-OF-SAMPLE.
// The results are shown as they are — no filtering for a nice win rate.
import type { Candle } from '../types.js';
import { DEFAULT_SETUP_PARAMS, LevelSetupEngine, type Setup, type SetupParams } from './setupEngine.js';
import { median } from '../volumeStats.js';

export interface Stats {
  total: number;
  long: number;
  short: number;
  closed: number;
  winRate: number;
  lossRate: number;
  avgR: number;
  medianR: number;
  profitFactor: number;
  expectancy: number;
  maxLosingStreak: number;
  avgMaeR: number;
  avgMfeR: number;
}

export function stats(setups: readonly Setup[]): Stats {
  const closed = setups.filter((s) => s.outcome.status !== 'open');
  const rs = closed.map((s) => s.outcome.r);
  const wins = closed.filter((s) => s.outcome.r > 0);
  const losses = closed.filter((s) => s.outcome.r <= 0);
  const gw = wins.reduce((a, s) => a + s.outcome.r, 0);
  const gl = -losses.reduce((a, s) => a + s.outcome.r, 0);
  let streak = 0;
  let maxStreak = 0;
  for (const s of [...closed].sort((a, b) => a.t - b.t)) {
    streak = s.outcome.r <= 0 ? streak + 1 : 0;
    maxStreak = Math.max(maxStreak, streak);
  }
  const avg = rs.length ? rs.reduce((a, x) => a + x, 0) / rs.length : 0;
  return {
    total: setups.length,
    long: setups.filter((s) => s.direction === 'LONG').length,
    short: setups.filter((s) => s.direction === 'SHORT').length,
    closed: closed.length,
    winRate: closed.length ? wins.length / closed.length : 0,
    lossRate: closed.length ? losses.length / closed.length : 0,
    avgR: avg,
    medianR: rs.length ? median(rs) : 0,
    profitFactor: gl > 0 ? gw / gl : gw > 0 ? Infinity : 0,
    expectancy: avg,
    maxLosingStreak: maxStreak,
    avgMaeR: closed.length ? closed.reduce((a, s) => a + s.outcome.maeR, 0) / closed.length : 0,
    avgMfeR: closed.length ? closed.reduce((a, s) => a + s.outcome.mfeR, 0) / closed.length : 0,
  };
}

export function groupStats(setups: readonly Setup[], key: (s: Setup) => string): Record<string, Stats> {
  const g = new Map<string, Setup[]>();
  for (const s of setups) g.set(key(s), [...(g.get(key(s)) ?? []), s]);
  return Object.fromEntries([...g.entries()].map(([k, v]) => [k, stats(v)]));
}

export const scoreBucket = (s: Setup): string => {
  const q = s.setupQuality;
  return q >= 80 ? '80–100' : q >= 65 ? '65–79' : q >= 50 ? '50–64' : '0–49';
};

/** Run the engine bar by bar over the history. `dailyBefore` must end before the first bar's day. */
export function runHistory(meta: { symbol: string; exchange: string; tick: number }, dailyBefore: readonly Candle[], bars: readonly Candle[], p: SetupParams): { setups: Setup[]; engine: LevelSetupEngine } {
  const eng = new LevelSetupEngine(meta, dailyBefore, p);
  for (const b of bars) eng.step(b);
  return { setups: eng.setups, engine: eng };
}

export interface WalkForwardResult {
  params: SetupParams;
  chosenBy: string;
  grid: { params: Partial<SetupParams>; train: Stats }[];
  segments: { name: 'TRAIN' | 'VALIDATION' | 'OOS'; from: number; to: number; stats: Stats }[];
  setups: Setup[];
}

export const GRID: Partial<SetupParams>[] = (() => {
  const out: Partial<SetupParams>[] = [];
  for (const volDecayMax of [0.6, 0.7, 0.85])
    for (const reactVolMin of [1.5, 1.8, 2.2]) for (const approachAtr of [0.6, 1.0]) out.push({ volDecayMax, reactVolMin, approachAtr });
  return out;
})();

/**
 * Walk-forward: the full history is replayed once per candidate parameter set (the engine is stateful
 * and needs its warm-up), but ONLY setups inside TRAIN are used to choose. The chosen set is then
 * reported separately on VALIDATION and OUT-OF-SAMPLE.
 */
export function walkForward(meta: { symbol: string; exchange: string; tick: number }, dailyBefore: readonly Candle[], bars: readonly Candle[], base: SetupParams = DEFAULT_SETUP_PARAMS, minTrades = 8): WalkForwardResult {
  const t0 = bars[0]?.t ?? 0;
  const tN = bars[bars.length - 1]?.t ?? 0;
  const trainEnd = t0 + (tN - t0) * 0.5;
  const valEnd = t0 + (tN - t0) * 0.75;
  const seg = (s: Setup): 'TRAIN' | 'VALIDATION' | 'OOS' => (s.t < trainEnd ? 'TRAIN' : s.t < valEnd ? 'VALIDATION' : 'OOS');
  const grid: WalkForwardResult['grid'] = [];
  let best: { p: SetupParams; score: number } | null = null;
  for (const g of GRID) {
    const p = { ...base, ...g };
    // only bars up to the end of TRAIN are replayed for the choice: nothing after TRAIN can influence it
    const trainBars = bars.filter((b) => b.t < trainEnd);
    const { setups } = runHistory(meta, dailyBefore, trainBars, p);
    const st = stats(setups);
    grid.push({ params: g, train: st });
    const score = st.closed >= minTrades ? st.expectancy : -Infinity;
    if (!best || score > best.score) best = { p, score };
  }
  const chosen = best && isFinite(best.score) ? best.p : base;
  const chosenBy = best && isFinite(best.score) ? 'max expectancy on TRAIN (≥ ' + minTrades + ' closed setups)' : `default parameters (no grid point had ≥ ${minTrades} closed setups on TRAIN)`;
  const { setups } = runHistory(meta, dailyBefore, bars, chosen);
  for (const s of setups) s.segment = seg(s);
  const segs: WalkForwardResult['segments'] = [
    { name: 'TRAIN', from: t0, to: trainEnd, stats: stats(setups.filter((s) => s.segment === 'TRAIN')) },
    { name: 'VALIDATION', from: trainEnd, to: valEnd, stats: stats(setups.filter((s) => s.segment === 'VALIDATION')) },
    { name: 'OOS', from: valEnd, to: tN, stats: stats(setups.filter((s) => s.segment === 'OOS')) },
  ];
  return { params: chosen, chosenBy, grid, segments: segs, setups };
}
