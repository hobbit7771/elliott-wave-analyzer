// Batch experiments for the Gerchik engine: several coins, time split TRAIN 50% / VALIDATION 25% / OOS 25%.
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { GerchikEngine, DEFAULT_GERCHIK_PARAMS, type GerchikParams } from '../../src/core/levelEngine/gerchik.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
import type { Candle } from '../../src/core/types.ts';
import type { Setup } from '../../src/core/levelEngine/setupEngine.ts';

const D = process.env.DATA ?? 'data';
const toC = (r: number[]): Candle => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 });
export interface Coin { sym: string; bars: Candle[]; daily: Candle[]; t0: number; tN: number; cache: Map<string, any> }
export function loadCoins(filter?: string[]): Coin[] {
  const syms = readdirSync(D).filter((f) => f.endsWith('_15m.json')).map((f) => f.replace('_15m.json', '')).filter((s) => !filter || filter.includes(s));
  return syms.map((sym) => {
    const bars = (JSON.parse(readFileSync(`${D}/${sym}_15m.json`, 'utf8')) as number[][]).map(toC);
    const d1 = (JSON.parse(readFileSync(`${D}/${sym}_1d.json`, 'utf8')) as number[][]).map(toC);
    const first = Math.floor(bars[0].t / 86400000) * 86400000;
    return { sym, bars, daily: d1.filter((d) => d.t < first), t0: bars[0].t, tN: bars[bars.length - 1].t, cache: new Map() };
  });
}
export function run(c: Coin, p: GerchikParams): Setup[] {
  const tick = c.bars[0].c * 1e-5;
  const e = new GerchikEngine({ symbol: c.sym, exchange: 'bybit-linear', tick }, c.daily, p, undefined, 3000, c.cache);
  for (const b of c.bars) e.step(b);
  return e.setups;
}
export const seg = (c: Coin, s: Setup) => { const f = (s.t - c.t0) / (c.tN - c.t0); return f < 0.5 ? 'TRAIN' : f < 0.75 ? 'VALIDATION' : 'OOS'; };
