import { readFileSync } from 'node:fs';
import { loadCoins } from './exp.ts';
import { runSite, stats } from '../../src/core/levelEngine/backtest.ts';
import { SITE_PARAMS, SITE_CHOICE } from '../../src/core/levelEngine/siteEngine.ts';
const btc = (JSON.parse(readFileSync((process.env.DATA ?? 'data') + '/BTCUSDT_1d.json', 'utf8')) as number[][]).map((r) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 }));
const all: any[] = []; const seg: any = { TRAIN: [], VALIDATION: [], OOS: [] }; const byM: any = {};
for (const c of loadCoins()) { const r = runSite({ symbol: c.sym, exchange: 'x', tick: c.bars[0].c * 1e-5 }, c.daily, c.bars, SITE_PARAMS, SITE_CHOICE, btc); const cl = r.setups.filter((x) => x.outcome.status !== 'open'); all.push(...cl); for (const x of cl) { seg[x.segment].push(x); (byM[x.reasons[1]] ??= []).push(x); } }
const st = stats(all); console.log('SITE n', st.closed, 'avgR', st.avgR.toFixed(3), 'sumR', all.reduce((a, x) => a + x.outcome.r, 0).toFixed(0), 'PF', st.profitFactor.toFixed(2), '|', Object.entries(seg).map(([k, v]: any) => k + ' ' + v.length + ' ' + stats(v).avgR.toFixed(2)).join(' | '));
console.log('  by model:', Object.entries(byM).map(([k, v]: any) => k + ' ' + v.length + ' ' + stats(v).avgR.toFixed(2)).join(' | '));
