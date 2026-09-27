import { writeFileSync } from 'node:fs';
import { loadCoins, seg } from './exp.ts';
import { RaschkeEngine, BOOK_RASCHKE_PARAMS, type RaschkeParams, type RaschkeSetup } from '../../src/core/levelEngine/raschke.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
const coins = loadCoins();
const setup = process.env.SETUP as RaschkeSetup;
const space: Record<string, any[]> = JSON.parse(process.env.SPACE!);
for (const [k, v] of Object.entries(space)) space[k] = v.map((x) => (x === 'inf' ? Infinity : x));
const keys = Object.keys(space);
const combos: any[] = [{}];
for (const k of keys) { const next: any[] = []; for (const c of combos) for (const v of space[k]) next.push({ ...c, [k]: v }); combos.splice(0, combos.length, ...next); }
const out: any[] = [];
for (const g of combos) {
  const p: RaschkeParams = { ...BOOK_RASCHKE_PARAMS, ...g, setups: [setup] };
  const by: any = { TRAIN: [], VALIDATION: [], OOS: [] };
  for (const c of coins) { const e = new RaschkeEngine({ symbol: c.sym, exchange: 'x', tick: 1e-9 }, c.daily, p); for (const b of c.bars) e.step(b); for (const x of e.setups) if (x.outcome.status !== 'open') by[seg(c, x)].push(x); }
  out.push({ g, seg: Object.fromEntries(Object.entries(by).map(([k, v]: any) => { const st = stats(v); return [k, { n: st.closed, avgR: st.avgR, win: st.winRate, pf: st.profitFactor }]; })) });
}
writeFileSync(process.env.OUT!, JSON.stringify(out));
console.error(setup, combos.length, 'combos');
