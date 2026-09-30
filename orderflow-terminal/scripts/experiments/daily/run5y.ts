// The site engine (SiteEngine: trend breakout + Gerchik levels, funding filter, BTC filter) on 5+ years of Binance 15m bars.
import { readFileSync, existsSync, writeFileSync } from 'node:fs';
import { runSite, stats } from '/home/user/elliott-wave-analyzer/orderflow-terminal/src/core/levelEngine/backtest.ts';
import { SITE_PARAMS, SITE_CHOICE } from '/home/user/elliott-wave-analyzer/orderflow-terminal/src/core/levelEngine/siteEngine.ts';
const D = 'data5y', DAY = 86400000, START = Date.UTC(2021, 5, 1);
const toC = (r: number[]) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 });
const load = (f: string) => (JSON.parse(readFileSync(`${D}/${f}`, 'utf8')) as number[][]).map(toC);
const btc = load('BTCUSDT_1d.json');
const SYMS = 'BTC ETH SOL XRP DOGE BNB ADA LINK AVAX SUI UNI INJ'.split(' ');
const out: any[] = [];
for (const s of SYMS) {
  const d1 = load(`${s}USDT_1d.json`); const all = load(`${s}USDT_15m.json`);
  const t0 = Math.max(START, Math.floor((d1[0].t + 120 * DAY) / DAY) * DAY);   // at least 120 days of daily history first
  const bars = all.filter((b) => b.t >= t0); const daily = d1.filter((d) => d.t < t0);
  const fp = `fund/${s}.json`;
  const f = existsSync(fp) ? (JSON.parse(readFileSync(fp, 'utf8')) as number[][]).map(([d, v]) => ({ t: d * DAY, rate: v / 1e6 })) : [];
  const t = Date.now();
  const r = runSite({ symbol: s + 'USDT', exchange: 'x', tick: bars[0].c * 1e-5 }, daily, bars, SITE_PARAMS, SITE_CHOICE, btc, f);
  const cl = r.setups.filter((x: any) => x.outcome.status !== 'open');
  for (const x of cl) out.push({ sym: s, t: x.t, model: x.reasons[1], dir: x.direction, r: x.outcome.r, closed: x.outcome.closedAt });
  console.log(s, new Date(t0).toISOString().slice(0, 10), 'bars', bars.length, 'setups', cl.length, 'avgR', stats(cl).avgR.toFixed(2), (Date.now() - t) + 'ms');
}
writeFileSync('run5y.json', JSON.stringify(out));
