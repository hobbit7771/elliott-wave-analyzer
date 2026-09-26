import { writeFileSync } from 'node:fs';
import { loadCoins, run, seg } from './exp.ts';
import { DEFAULT_GERCHIK_PARAMS, type GerchikParams } from '../../src/core/levelEngine/gerchik.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
const coins = loadCoins((process.env.COINS ?? '').split(',').filter(Boolean).length ? process.env.COINS!.split(',') : undefined);
const N = +(process.env.N ?? 600);
let seed = +(process.env.SEED ?? 7);
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const pick = <T,>(a: T[]) => a[Math.floor(rnd() * a.length)];
const space = JSON.parse(process.env.SPACE ?? '{}') as Record<string, any[]>;
for (const [k, v] of Object.entries(space)) space[k] = v.map((x: any) => (x === 'inf' ? Infinity : x));
const out: any[] = [];
const t0 = Date.now();
for (let n = 0; n < N; n++) {
  const g: any = Object.fromEntries(Object.entries(space).map(([k, v]) => [k, pick(v as any[])]));
  const p: GerchikParams = { ...DEFAULT_GERCHIK_PARAMS, ...g, bounce: g.models.includes('B'), falseBreak: g.models.includes('F'), breakout: g.models.includes('K') };
  const res: any = { g, seg: {} as any, coin: {} as any };
  const all: any = { TRAIN: [], VALIDATION: [], OOS: [] };
  for (const c of coins) {
    const s = run(c, p);
    const by: any = { TRAIN: [], VALIDATION: [], OOS: [] };
    for (const x of s) if (x.outcome.status !== 'open') by[seg(c, x)].push(x);
    for (const k of Object.keys(by)) all[k].push(...by[k]);
    res.coin[c.sym] = Object.fromEntries(Object.entries(by).map(([k, v]: any) => [k, { n: v.length, avgR: stats(v).avgR }]));
  }
  for (const k of Object.keys(all)) { const st = stats(all[k]); res.seg[k] = { n: st.closed, avgR: st.avgR, win: st.winRate, pf: st.profitFactor }; }
  out.push(res);
  if (n % 50 === 49) console.error(n + 1, 'configs', Math.round((Date.now() - t0) / 1000) + 's');
}
writeFileSync(process.env.OUT ?? 'grid.json', JSON.stringify(out));
