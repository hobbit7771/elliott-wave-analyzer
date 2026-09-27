import { loadCoins, seg } from './exp.ts';
import { RaschkeEngine, BOOK_RASCHKE_PARAMS, type RaschkeParams, type RaschkeSetup } from '../../src/core/levelEngine/raschke.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
import type { Setup } from '../../src/core/levelEngine/setupEngine.ts';
const cfg = JSON.parse(process.env.CFG ?? '{}');
for (const k of Object.keys(cfg)) if (cfg[k] === 'inf') cfg[k] = Infinity;
const coins = loadCoins();
const f = (x: number) => (x >= 0 ? '+' : '') + x.toFixed(2);
const only = (process.env.SETUPS ?? BOOK_RASCHKE_PARAMS.setups.join(',')).split(',') as RaschkeSetup[];
const perSetup = process.env.JOINT ? [only] : only.map((s) => [s]);
for (const list of perSetup) {
  const p: RaschkeParams = { ...BOOK_RASCHKE_PARAMS, ...cfg, setups: list };
  const pool: Record<string, Setup[]> = { TRAIN: [], VALIDATION: [], OOS: [], ALL: [] };
  let gross = 0, n = 0, bars = 0;
  const perCoin: string[] = [];
  for (const c of coins) {
    const e = new RaschkeEngine({ symbol: c.sym, exchange: 'bybit-linear', tick: c.bars[0].c * 1e-5 }, c.daily, p);
    for (const b of c.bars) e.step(b);
    const s = e.setups.filter((x) => x.outcome.status !== 'open');
    for (const x of s) { pool[seg(c, x)].push(x); pool.ALL.push(x); }
    perCoin.push(c.sym.replace('USDT', '') + ' ' + s.length + ' ' + f(stats(s).avgR));
  }
  // gross R: add back the costs (entry taker + exit fee) approximately via rerun without fees
  const pg: RaschkeParams = { ...p, feeMaker: 0, feeTaker: 0, slippage: 0 };
  const allG: Setup[] = [];
  for (const c of coins) { const e = new RaschkeEngine({ symbol: c.sym, exchange: 'x', tick: 1e-9 }, c.daily, pg); for (const b of c.bars) e.step(b); allG.push(...e.setups.filter((x) => x.outcome.status !== 'open')); }
  const A = stats(pool.ALL);
  const L = stats(pool.ALL.filter((x) => x.direction === 'LONG')), Sx = stats(pool.ALL.filter((x) => x.direction === 'SHORT'));
  const medRisk = pool.ALL.map((x) => Math.abs(x.entry - x.sl) / x.entry * 100).sort((a, b) => a - b)[Math.floor(pool.ALL.length / 2)] ?? NaN;
  console.log(list.join('+').padEnd(22), 'n', String(A.closed).padStart(4), 'avgR', f(A.avgR), 'gross', f(stats(allG).avgR), 'win', (A.winRate * 100).toFixed(0).padStart(2) + '%', 'PF', A.profitFactor.toFixed(2), '| T', pool.TRAIN.length, f(stats(pool.TRAIN).avgR), 'V', pool.VALIDATION.length, f(stats(pool.VALIDATION).avgR), 'O', pool.OOS.length, f(stats(pool.OOS).avgR), '| L', L.closed, f(L.avgR), 'S', Sx.closed, f(Sx.avgR), '| med stop', medRisk.toFixed(2) + '%');
  if (process.env.COINS_DETAIL) console.log('   ', perCoin.join(' | '));
}
