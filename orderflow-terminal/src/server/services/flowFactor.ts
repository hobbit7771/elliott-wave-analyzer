// Paper forward test of the order-flow factor flow7 (src/core/flowFactor.ts) on Binance USDT-M perpetuals — the only
// free source with taker-buy volume in daily bars. Weekly rebalance at 00:05 UTC, hourly marks, funding accrued from
// the settlement history every 4 h. Not part of the managed portfolio. State in oft.settings 'flow:state'.
import type { Repo } from '../persist/repo.js';
import type { BinanceFuturesAdapter } from '../adapters/binance.js';
import type { Funding } from '../../core/carry.js';
import { accrueFactor, applyTargets, factorEquity, newFactorState, type FactorState } from '../../core/fundingFactor.js';
import { FLOW_BACKTEST, SITE_FLOW_PARAMS, flowFeatures, flowTargets, type FlowInput } from '../../core/flowFactor.js';
import { trackVsBacktest } from '../../core/portfolio.js';

const KEY = 'flow:state';
const DAY = 86_400_000;
const STABLE = /^(USDC|FDUSD|TUSD|BUSD|USDP|DAI|USDE|EUR|AEUR)USDT$/;

export class FlowFactorService {
  private st: FactorState | null = null;
  private prices = new Map<string, number>();
  private funding = new Map<string, Funding[]>();
  private lastPrices = 0;
  private lastFunding = 0;
  private tried = 0; // day of the last rebalance attempt
  private busy = false;
  private scores: { t: number; rows: { symbol: string; flow7: number }[] } | null = null;
  error = '';

  constructor(private deps: { adapter: () => BinanceFuturesAdapter; repo: () => Repo | null; log: (m: string) => void }) {}

  async tick(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const repo = this.deps.repo();
      if (!this.st) {
        const saved = repo ? await repo.getSetting<FactorState>(KEY).catch(() => undefined) : undefined;
        this.st = saved ?? newFactorState(Date.now(), SITE_FLOW_PARAMS);
      }
      const st = this.st;
      const ad = this.deps.adapter();
      const now = Date.now();
      if (now - this.lastPrices >= 10 * 60_000) {
        this.prices = new Map(Object.entries(await ad.fetchPrices()));
        this.lastPrices = now;
      }
      if (st.positions.length && now - this.lastFunding >= 4 * 3600_000) {
        for (const pos of st.positions) {
          try {
            this.funding.set(pos.symbol, await ad.fetchFunding(pos.symbol, pos.lastSettle + 1));
          } catch (e) {
            this.deps.log(`[flow] funding ${pos.symbol}: ${(e as Error).message.slice(0, 100)}`);
          }
          await new Promise((r) => setTimeout(r, 150));
        }
        this.lastFunding = now;
      }
      accrueFactor(st, new Map(st.positions.map((x) => [x.symbol, { price: this.prices.get(x.symbol) ?? x.px, settlements: this.funding.get(x.symbol) ?? [] }])), now);
      const day = Math.floor(now / DAY) * DAY;
      const due = !st.lastRebalance || day >= st.lastRebalance + st.params.rebalanceDays * DAY;
      if (due && now >= day + 5 * 60_000 && this.tried !== day) {
        this.tried = day;
        await this.rebalance(now);
      }
      const last = st.equity[st.equity.length - 1];
      if (!last || now - last.t >= 3600_000) {
        st.equity.push({ t: now, eq: factorEquity(st, (s) => this.prices.get(s)) });
        if (st.equity.length > 24 * 400) st.equity.shift();
      }
      if (repo) await repo.setSetting(KEY, st).catch((e) => this.deps.log(`[flow] state save failed: ${(e as Error).message}`));
      this.error = '';
    } catch (e) {
      this.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[flow] update failed: ${this.error}`);
    } finally {
      this.busy = false;
    }
  }

  private async rebalance(now: number): Promise<void> {
    const st = this.st!;
    const ad = this.deps.adapter();
    // candidates: the 80 most traded crypto USDT perpetuals of the last 24 h (stock / commodity / index contracts are
    // excluded: the research universe had none); the universe is then the top 50 by 30-day volume
    const crypto = new Set((await ad.listInstruments()).filter((m) => m.quote === 'USDT' && !m.note).map((m) => m.symbol));
    const tickers = (await ad.fetchTickers()).filter((x) => crypto.has(x.symbol) && !STABLE.test(x.symbol)).sort((a, b) => b.turnover - a.turnover).slice(0, 80);
    const inputs: FlowInput[] = [];
    for (const x of tickers) {
      try {
        const k = (await ad.fetchKlines(x.symbol, '1d', 40)).filter((c) => c.t + DAY <= now);
        const f = flowFeatures(k);
        inputs.push({ symbol: x.symbol, price: this.prices.get(x.symbol) ?? x.last, ...f });
      } catch (e) {
        this.deps.log(`[flow] klines ${x.symbol}: ${(e as Error).message.slice(0, 100)}`);
      }
      await new Promise((r) => setTimeout(r, 150));
    }
    const p = SITE_FLOW_PARAMS;
    const uni = inputs.filter((x) => isFinite(x.flow7) && isFinite(x.qv30)).sort((a, b) => b.qv30 - a.qv30).slice(0, p.universe);
    this.scores = { t: now, rows: uni.map((x) => ({ symbol: x.symbol, flow7: x.flow7 })).sort((a, b) => b.flow7 - a.flow7) };
    const tg = flowTargets(inputs, p);
    if (!tg) {
      this.deps.log(`[flow] rebalance skipped: ${uni.length} coins with data`);
      return;
    }
    applyTargets(st, tg, new Map(inputs.map((x) => [x.symbol, x.price])), now);
    this.deps.log(`OFT_FLOW ${JSON.stringify({ long: st.last?.long, short: st.last?.short, equity: +factorEquity(st, (s) => this.prices.get(s)).toFixed(2) })}`);
  }

  view(): unknown {
    const st = this.st;
    if (!st) return { status: 'loading', error: this.error };
    const equity = factorEquity(st, (s) => this.prices.get(s));
    const days = (Date.now() - st.startedAt) / DAY;
    const live = equity / st.params.capital - 1;
    let peak = -Infinity;
    let dd = 0;
    for (const e of st.equity) {
      peak = Math.max(peak, e.eq);
      dd = Math.max(dd, 1 - e.eq / peak);
    }
    return {
      status: 'ok',
      error: this.error,
      startedAt: st.startedAt,
      lastRebalance: st.lastRebalance,
      equity,
      returnPct: live * 100,
      maxDrawdown: dd,
      backtest: FLOW_BACKTEST,
      tracking: trackVsBacktest(live, Math.floor(days), FLOW_BACKTEST.annRet, FLOW_BACKTEST.annVol),
      last: st.last,
      positions: st.positions.map((x) => {
        const px = this.prices.get(x.symbol) ?? x.px;
        return { symbol: x.symbol, qty: x.qty, notional: x.qty * px, funding: x.funding, fees: x.fees };
      }),
      scores: this.scores,
      equityPath: st.equity.slice(-24 * 90),
    };
  }
}
