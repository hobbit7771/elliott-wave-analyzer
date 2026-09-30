// Paper portfolio over the site's strategies with the risk manager of src/core/portfolio.ts: hourly snapshots of the
// three sleeves (site-engine trades in R, carry equity, funding-factor equity), daily returns, the volatility-based
// multiplier for the directional part, the managed equity and per-coin exposure limits. State in oft.settings.
import type { Repo } from '../persist/repo.js';
import type { LevelService } from './levelService.js';
import type { CarryService } from './carry.js';
import { BACKTEST, BACKTEST_DD, drawdownVerdict, SITE_PORTFOLIO_PARAMS, trackVsBacktest, dailyReturns, exposures, managedEquity, maxDrawdown, riskMultiplier, type PortfolioParams, type Snap } from '../../core/portfolio.js';

const KEY = 'portfolio:state';

interface State {
  params: PortfolioParams;
  startedAt: number;
  snaps: Snap[];
}

export class PortfolioService {
  private st: State | null = null;
  error = '';

  constructor(private deps: { levels: LevelService; carry: CarryService; repo: () => Repo | null; log: (m: string) => void }) {}

  async tick(): Promise<void> {
    try {
      const repo = this.deps.repo();
      if (!this.st) {
        const saved = repo ? await repo.getSetting<State>(KEY).catch(() => undefined) : undefined;
        // a changed configuration (e.g. the trend risk) starts a new record instead of mixing two configurations
        const same = saved && JSON.stringify(saved.params) === JSON.stringify(SITE_PORTFOLIO_PARAMS);
        if (saved && !same) this.deps.log(`[portfolio] parameters changed: new record from now (was ${JSON.stringify(saved.params)})`);
        this.st = same && saved ? saved : { params: SITE_PORTFOLIO_PARAMS, startedAt: Date.now(), snaps: [] };
      }
      const sl = this.deps.carry.sleeves();
      if (!sl.ready) return; // quotes not loaded yet: no snapshot rather than a wrong one
      const now = Date.now();
      const last = this.st.snaps[this.st.snaps.length - 1];
      if (last && now - last.t < 3600_000) return;
      const tr = this.deps.levels.portfolioInputs(this.st.startedAt);
      this.st.snaps.push({ t: now, trendR: tr.trendR, carry: sl.carry, factor: sl.factor });
      if (this.st.snaps.length > 24 * 400) this.st.snaps.shift();
      if (repo) await repo.setSetting(KEY, this.st).catch((e) => this.deps.log(`[portfolio] state save failed: ${(e as Error).message}`));
      this.error = '';
    } catch (e) {
      this.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[portfolio] update failed: ${this.error}`);
    }
  }

  view(): unknown {
    const st = this.st;
    if (!st) return { status: 'loading', error: this.error };
    const p = st.params;
    const rets = dailyReturns(st.snaps, p);
    const path = managedEquity(rets, p);
    const k = riskMultiplier(rets, p);
    const equity = path.length ? path[path.length - 1].eq : p.capital;
    const tr = this.deps.levels.portfolioInputs(st.startedAt);
    const sl = this.deps.carry.sleeves();
    const items = [
      ...tr.open.map((o) => ({ symbol: o.symbol, sleeve: 'trend', notional: (o.dir * k * p.trendRisk * p.capital * o.price) / Math.max(1e-12, Math.abs(o.entry - o.stop)) })),
      ...sl.factorPositions.map((f) => ({ symbol: f.symbol, sleeve: 'factor', notional: f.notional * p.factorWeight * k * (p.capital / 10_000) })),
    ];
    const first = st.snaps[0];
    return {
      status: 'ok',
      error: this.error,
      params: p,
      startedAt: st.startedAt,
      days: rets.length,
      k,
      kActive: rets.length >= p.minHistoryDays,
      equity,
      returnPct: ((equity - p.capital) / p.capital) * 100,
      maxDrawdown: maxDrawdown(path),
      ddExpected: BACKTEST_DD,
      ddVerdict: drawdownVerdict(maxDrawdown(path)),
      sleeves: {
        trend: { r: tr.trendR, trades: tr.trades, pnl: tr.trendR * p.trendRisk * p.capital, open: tr.open },
        carry: { equity: sl.carry, pnlPct: first ? (sl.carry / first.carry - 1) * 100 : 0 },
        factor: { equity: sl.factor, pnlPct: first ? (sl.factor / first.factor - 1) * 100 : 0 },
      },
      exposures: exposures(items, equity, p).slice(0, 30),
      tracking: (() => {
        const days = rets.length;
        const sum = (f: (r: (typeof rets)[number]) => number) => rets.reduce((a, r) => a * (1 + f(r)), 1) - 1;
        const live = { trend: sum((r) => r.trend), carry: sum((r) => r.carry), factor: sum((r) => r.factor), total: (equity - p.capital) / p.capital };
        return (['trend', 'carry', 'factor', 'total'] as const).map((k) => ({ sleeve: k, note: BACKTEST[k].note, days, live: live[k], annRet: BACKTEST[k].annRet, ...trackVsBacktest(live[k], days, BACKTEST[k].annRet, BACKTEST[k].annVol) }));
      })(),
      journal: {
        trend: tr.journal,
        carry: [...sl.carryClosed].reverse(),
        factorLast: sl.factorLast,
      },
      path: path.slice(-400),
    };
  }
}
