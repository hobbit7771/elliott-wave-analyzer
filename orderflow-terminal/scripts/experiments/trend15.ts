import { readFileSync } from 'node:fs';
import { loadCoins, seg } from './exp.ts';
import { TrendEngine, SITE_TREND_PARAMS } from '../../src/core/levelEngine/trend.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
const btc = (JSON.parse(readFileSync(process.env.DATA + '/BTCUSDT_1d.json', 'utf8')) as number[][]).map((r) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 }));
const all: any[] = []; const by: any = { TRAIN: [], VALIDATION: [], OOS: [] }; const line: string[] = [];
for (const c of loadCoins()) {
  const e = new TrendEngine({ symbol: c.sym, exchange: 'x', tick: 1e-9 }, c.daily, { ...SITE_TREND_PARAMS, ...(process.env.CFG ? JSON.parse(process.env.CFG) : {}) }, btc);
  for (const b of c.bars) e.step(b);
  const s = e.setups.filter((x) => x.outcome.status !== 'open');
  all.push(...s); for (const x of s) by[seg(c, x)].push(x);
  line.push(c.sym.replace('USDT', '') + ' ' + s.length + ' ' + stats(s).avgR.toFixed(2));
}
const st = stats(all);
console.log('TREND 15m engine, 1y: n', st.closed, 'avgR', st.avgR.toFixed(2), 'sumR', all.reduce((a, x) => a + x.outcome.r, 0).toFixed(0), 'win', (st.winRate * 100).toFixed(0) + '%', 'PF', st.profitFactor.toFixed(2), '|', Object.entries(by).map(([k, v]: any) => k + ' ' + v.length + ' ' + stats(v).avgR.toFixed(2)).join(' | '));
console.log('  ', line.join(' | '));
