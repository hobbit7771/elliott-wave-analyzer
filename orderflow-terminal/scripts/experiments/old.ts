import { loadCoins, seg } from './exp.ts';
import { LevelSetupEngine, DEFAULT_SETUP_PARAMS } from '../../src/core/levelEngine/setupEngine.ts';
import { stats } from '../../src/core/levelEngine/backtest.ts';
const coins = loadCoins();
const pool: any = { TRAIN: [], VALIDATION: [], OOS: [], ALL: [] };
let wrong = 0, tot = 0;
for (const c of coins) {
  const e = new LevelSetupEngine({ symbol: c.sym, exchange: 'bybit-linear', tick: c.bars[0].c * 1e-5 }, c.daily, DEFAULT_SETUP_PARAMS);
  for (const b of c.bars) e.step(b);
  const s = e.setups.filter((x) => x.outcome.status !== 'open');
  // fees in R (taker in and out, as the old engine enters at the close)
  for (const x of s) x.outcome.r -= (0.00055 * 2 + 0.0002) * x.entry / Math.abs(x.entry - x.sl);
  for (const x of s) { pool[seg(c, x)].push(x); pool.ALL.push(x); tot++; if ((x.direction === 'LONG') !== (x.entry > x.level)) wrong++; }
}
for (const k of Object.keys(pool)) { const st = stats(pool[k]); console.log('OLD', k.padEnd(10), 'n', st.closed, 'avgR', st.avgR.toFixed(2), 'win', (st.winRate * 100).toFixed(0) + '%', 'PF', st.profitFactor.toFixed(2)); }
console.log('entries on the wrong side of their level (long below / short above):', wrong, 'of', tot);
