import { loadCoins } from './exp.ts';
import { runSite, stats } from '../../src/core/levelEngine/backtest.ts';
import { SITE_PARAMS, SITE_CHOICE } from '../../src/core/levelEngine/siteEngine.ts';
const all: any[] = []; const seg: any = { TRAIN: [], VALIDATION: [], OOS: [] };
for (const c of loadCoins()) { const r = runSite({ symbol: c.sym, exchange: 'x', tick: c.bars[0].c * 1e-5 }, c.daily, c.bars, SITE_PARAMS, SITE_CHOICE); const cl = r.setups.filter((x) => x.outcome.status !== 'open'); all.push(...cl); for (const x of cl) seg[x.segment].push(x); }
const st = stats(all); console.log('SITE n', st.closed, 'avgR', st.avgR.toFixed(3), 'PF', st.profitFactor.toFixed(2), Object.entries(seg).map(([k, v]: any) => k + ' ' + v.length + ' ' + stats(v).avgR.toFixed(2)).join(' | '));
