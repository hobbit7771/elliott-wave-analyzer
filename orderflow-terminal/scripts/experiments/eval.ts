import { loadCoins, run, seg } from './exp.ts';
import { DEFAULT_GERCHIK_PARAMS, type GerchikParams } from '../../src/core/levelEngine/gerchik.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
const cfg = JSON.parse(process.env.CFG ?? '{}');
for (const k of Object.keys(cfg)) if (cfg[k] === 'inf') cfg[k] = Infinity;
const p: GerchikParams = { ...DEFAULT_GERCHIK_PARAMS, ...cfg };
if (cfg.models) Object.assign(p, { bounce: cfg.models.includes('B'), falseBreak: cfg.models.includes('F'), breakout: cfg.models.includes('K') });
const coins = loadCoins((process.env.COINS ?? '').split(',').filter(Boolean).length ? process.env.COINS!.split(',') : undefined);
const f = (x: number) => (x >= 0 ? '+' : '') + x.toFixed(2);
const pool: any = { TRAIN: [], VALIDATION: [], OOS: [], ALL: [] };
for (const c of coins) {
  const s = run(c, p).filter((x) => x.outcome.status !== 'open');
  const row = ['TRAIN', 'VALIDATION', 'OOS'].map((k) => { const v = s.filter((x) => seg(c, x) === k); pool[k].push(...v); const st = stats(v); return k[0] + ' ' + String(st.closed).padStart(3) + ' ' + f(st.avgR); });
  pool.ALL.push(...s);
  const L = stats(s.filter((x) => x.direction === 'LONG')), Sh = stats(s.filter((x) => x.direction === 'SHORT'));
  const byM: any = {}; for (const x of s) (byM[x.reasons[1]] ??= []).push(x);
  console.log(c.sym.padEnd(9), row.join(' | '), '| L', L.closed, f(L.avgR), 'S', Sh.closed, f(Sh.avgR), '|', Object.entries(byM).map(([k, v]: any) => k + ' ' + v.length + ' ' + f(stats(v).avgR)).join(' '));
}
for (const k of Object.keys(pool)) { const st = stats(pool[k]); console.log('POOL', k.padEnd(10), 'n', st.closed, 'avgR', f(st.avgR), 'win', (st.winRate * 100).toFixed(0) + '%', 'PF', st.profitFactor.toFixed(2), 'maxLoseStreak', st.maxLosingStreak); }
