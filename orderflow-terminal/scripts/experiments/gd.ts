import { readFileSync, readdirSync } from 'node:fs';
import { GerchikEngine, SITE_GERCHIK_PARAMS } from '../../src/core/levelEngine/gerchik.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
const dir = process.env.DAILY ?? 'daily';
const toC = (r: number[]) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 });
const cfg = process.env.CFG ? JSON.parse(process.env.CFG) : {};
const p = { ...SITE_GERCHIK_PARAMS, ltfMs: 86_400_000, bpuWindow: 3, cooldownBars: 5, maxHoldBars: 30, ...cfg };
const all: any[] = []; const byYear: Record<string, number[]> = {};
for (const f of readdirSync(dir)) {
  const d = (JSON.parse(readFileSync(dir + '/' + f, 'utf8')) as number[][]).map(toC);
  if (d.length < 500) continue;
  const e = new GerchikEngine({ symbol: f, exchange: 'x', tick: d[0].c * 1e-5 }, d.slice(0, 400), p);
  for (const b of d.slice(400)) e.step(b);
  for (const s of e.setups) if (s.outcome.status !== 'open') { all.push(s); (byYear[new Date(s.t).getUTCFullYear()] ??= []).push(s.outcome.r); }
}
const st = stats(all);
console.log('Gerchik on DAILY bars, ' + JSON.stringify(cfg) + ': n', st.closed, 'avgR', st.avgR.toFixed(2), 'PF', st.profitFactor.toFixed(2), 'win', (st.winRate*100).toFixed(0) + '%', '| by year', Object.entries(byYear).map(([y, r]) => y + ':' + r.length + '/' + (r.reduce((a, b) => a + b, 0) / r.length).toFixed(2)).join(' '));
