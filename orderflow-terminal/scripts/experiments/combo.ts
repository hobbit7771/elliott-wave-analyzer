import { loadCoins, seg } from './exp.ts';
import { GerchikEngine, SITE_GERCHIK_PARAMS } from '../../src/core/levelEngine/gerchik.ts';
import { RaschkeEngine, BOOK_RASCHKE_PARAMS } from '../../src/core/levelEngine/raschke.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
import type { Setup } from '../../src/core/levelEngine/setupEngine.ts';
const coins = loadCoins();
const G = { ...SITE_GERCHIK_PARAMS, adxMin: 20 };
const H = { ...BOOK_RASCHKE_PARAMS, setups: ['HOLY_GRAIL'] as any, holdDays: 10, trailPrevDay: false, hgTf: 'D1' as const, hgAdx: 30 };
const f = (x: number) => (x >= 0 ? '+' : '') + x.toFixed(2);
const rows: Record<string, Record<string, Setup[]>> = { GERCHIK_ADX20: {}, HOLY_GRAIL: {}, COMBINED: {} };
for (const k of Object.keys(rows)) rows[k] = { TRAIN: [], VALIDATION: [], OOS: [], ALL: [] };
const coinLine: string[] = [];
for (const c of coins) {
  const g = new GerchikEngine({ symbol: c.sym, exchange: 'x', tick: c.bars[0].c * 1e-5 }, c.daily, G);
  const h = new RaschkeEngine({ symbol: c.sym, exchange: 'x', tick: c.bars[0].c * 1e-5 }, c.daily, H);
  for (const b of c.bars) { g.step(b); h.step(b); }
  const gs = g.setups.filter((x) => x.outcome.status !== 'open'), hs = h.setups.filter((x) => x.outcome.status !== 'open');
  for (const [k, list] of [['GERCHIK_ADX20', gs], ['HOLY_GRAIL', hs], ['COMBINED', [...gs, ...hs]]] as const) for (const x of list) { rows[k][seg(c, x)].push(x); rows[k].ALL.push(x); }
  coinLine.push(c.sym.replace('USDT', '') + ' ' + (gs.length + hs.length) + ' ' + f(stats([...gs, ...hs]).avgR));
}
for (const [k, r] of Object.entries(rows)) {
  const a = stats(r.ALL);
  // sum of R per month (portfolio view: 1R risk per trade)
  const months = new Map<string, number>();
  for (const x of r.ALL) { const m = new Date(x.t).toISOString().slice(0, 7); months.set(m, (months.get(m) ?? 0) + x.outcome.r); }
  const pos = [...months.values()].filter((v) => v > 0).length;
  console.log(k.padEnd(14), 'n', a.closed, 'avgR', f(a.avgR), 'sumR', f(r.ALL.reduce((s, x) => s + x.outcome.r, 0)), 'PF', a.profitFactor.toFixed(2), 'win', (a.winRate * 100).toFixed(0) + '%', 'maxLoseStreak', a.maxLosingStreak, '| T', r.TRAIN.length, f(stats(r.TRAIN).avgR), 'V', r.VALIDATION.length, f(stats(r.VALIDATION).avgR), 'O', r.OOS.length, f(stats(r.OOS).avgR), '| months +', pos + '/' + months.size);
}
console.log('per coin (combined):', coinLine.join(' | '));
