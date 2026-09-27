import { readFileSync, existsSync } from 'node:fs';
import { loadCoins } from './exp.ts';
import { runSite, stats } from '../../src/core/levelEngine/backtest.ts';
import { SITE_PARAMS, SITE_CHOICE } from '../../src/core/levelEngine/siteEngine.ts';
const btc = (JSON.parse(readFileSync((process.env.DATA ?? 'data') + '/BTCUSDT_1d.json', 'utf8')) as number[][]).map((r) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 }));
for (const useF of [false, true]) {
  const all: any[] = []; const byM: any = {}; let skipped = 0;
  for (const c of loadCoins()) {
    const s = c.sym.replace('USDT', ''); const fp = `fund/${s}.json`;
    const f = useF && existsSync(fp) ? (JSON.parse(readFileSync(fp, 'utf8')) as number[][]).map(([d, v]) => ({ t: d * 86400000, rate: v / 1e6 })) : [];
    const r = runSite({ symbol: c.sym, exchange: 'x', tick: c.bars[0].c * 1e-5 }, c.daily, c.bars, SITE_PARAMS, SITE_CHOICE, btc, f);
    const cl = r.setups.filter((x) => x.outcome.status !== 'open'); all.push(...cl); for (const x of cl) (byM[x.reasons[1]] ??= []).push(x);
  }
  const st = stats(all);
  console.log(useF ? 'WITH funding filter   ' : 'WITHOUT funding filter', 'n', st.closed, 'avgR', st.avgR.toFixed(3), 'sumR', all.reduce((a, x) => a + x.outcome.r, 0).toFixed(1), 'PF', st.profitFactor.toFixed(2), '| by model:', Object.entries(byM).map(([k, v]: any) => k + ' ' + v.length + ' ' + stats(v).avgR.toFixed(2) + ' sum ' + v.reduce((a: number, x: any) => a + x.outcome.r, 0).toFixed(1)).join(' | '));
}
