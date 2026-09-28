// Paper funding strategies on Bybit: carry (long spot + short perpetual, src/core/carry.ts) and the cross-sectional
// funding factor (long lowest-funding / short highest-funding perpetuals, src/core/fundingFactor.ts).
// Quotes are refreshed every few minutes, funding history of the universe every 4 h; the daily decision and the
// accrual of settled funding run on every update; the state is kept in Supabase (oft.settings 'carry:state').
import type { Repo } from '../persist/repo.js';
import type { BybitLinearAdapter } from '../adapters/bybit.js';
import { SITE_FACTOR_PARAMS, accrueFactor, factorEquity, meanFunding, newFactorState, rebalance, type FactorInput, type FactorState } from '../../core/fundingFactor.js';
import { SITE_CARRY_PARAMS, accrue, decide, newCarryState, totals, trailingApr, basisPnl, type CarryQuote, type CarryState, type Funding } from '../../core/carry.js';

const KEY = 'carry:state';
const FKEY = 'factor:state';
const DAY = 86_400_000;
const UNIVERSE = 40; // the most liquid USDT perpetuals that also have a spot pair

export class CarryService {
  private st: CarryState | null = null;
  private fs: FactorState | null = null;
  private factorTried = 0; // day of the last rebalance attempt (klines are fetched at most once a day)
  private quotes = new Map<string, CarryQuote>();
  private hist = new Map<string, { at: number; f: Funding[] }>();
  private lastQuotes = 0;
  private busy = false;
  error = '';

  constructor(private deps: { adapter: () => BybitLinearAdapter; repo: () => Repo | null; log: (m: string) => void }) {}

  async tick(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const repo = this.deps.repo();
      if (!this.st) {
        const saved = repo ? await repo.getSetting<CarryState>(KEY).catch(() => undefined) : undefined;
        this.st = saved ?? newCarryState(Date.now(), SITE_CARRY_PARAMS);
        const fsaved = repo ? await repo.getSetting<FactorState>(FKEY).catch(() => undefined) : undefined;
        this.fs = fsaved ?? newFactorState(Date.now(), SITE_FACTOR_PARAMS);
      }
      await this.refresh();
      const now = Date.now();
      const st = this.st;
      accrue(st, this.quotes, now);
      const decided = decide(st, this.quotes, now);
      const tot = totals(st, this.quotes);
      const last = st.equity[st.equity.length - 1];
      if (!last || now - last.t >= 3600_000) {
        st.equity.push({ t: now, eq: tot.equity });
        if (st.equity.length > 24 * 400) st.equity.shift();
      }
      if (decided) this.deps.log(`OFT_CARRY ${JSON.stringify({ positions: st.positions.map((p) => p.symbol), equity: +tot.equity.toFixed(2), closed: st.closed.length })}`);
      if (repo) await repo.setSetting(KEY, st).catch((e) => this.deps.log(`[carry] state save failed: ${(e as Error).message}`));
      await this.factorTick(now);
      if (repo && this.fs) await repo.setSetting(FKEY, this.fs).catch((e) => this.deps.log(`[factor] state save failed: ${(e as Error).message}`));
      this.error = '';
    } catch (e) {
      this.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[carry] update failed: ${this.error}`);
    } finally {
      this.busy = false;
    }
  }

  private perp(s: string): number | undefined {
    const q = this.quotes.get(s);
    return q && q.perp > 0 ? q.perp : undefined;
  }

  private async factorTick(now: number): Promise<void> {
    const fs = this.fs;
    if (!fs) return;
    accrueFactor(fs, new Map([...this.quotes].map(([s, q]) => [s, { price: q.perp, settlements: q.settlements }])), now);
    const day = Math.floor(now / DAY) * DAY;
    if (now >= day + 5 * 60_000 && this.factorTried !== day && this.quotes.size && (!fs.lastRebalance || day >= fs.lastRebalance + fs.params.rebalanceDays * DAY)) {
      this.factorTried = day;
      // volatility of daily returns (30 d) for the inverse-vol weights: fetched only on rebalance days
      const ad = this.deps.adapter();
      const inputs: FactorInput[] = [];
      for (const q of this.quotes.values()) {
        try {
          const k = (await ad.fetchKlines(q.symbol, '1d', 35)).filter((x) => x.t + DAY <= now);
          const r = k.slice(1).map((x, i) => Math.log(x.c / k[i].c));
          const m = r.reduce((a, b) => a + b, 0) / (r.length || 1);
          const vol = r.length >= 20 ? Math.sqrt(r.reduce((a, b) => a + (b - m) ** 2, 0) / (r.length - 1)) * Math.sqrt(365) : NaN;
          inputs.push({ symbol: q.symbol, price: q.perp, settlements: q.settlements, vol });
        } catch (e) {
          this.deps.log(`[factor] klines ${q.symbol}: ${(e as Error).message.slice(0, 100)}`);
        }
        await new Promise((r) => setTimeout(r, 100));
      }
      if (rebalance(fs, inputs, now)) this.deps.log(`OFT_FACTOR ${JSON.stringify({ long: fs.last?.long, short: fs.last?.short, equity: +factorEquity(fs, (s) => this.perp(s)).toFixed(2) })}`);
    }
    const last = fs.equity[fs.equity.length - 1];
    if (!last || now - last.t >= 3600_000) {
      fs.equity.push({ t: now, eq: factorEquity(fs, (s) => this.perp(s)) });
      if (fs.equity.length > 24 * 400) fs.equity.shift();
    }
  }

  private async refresh(): Promise<void> {
    const now = Date.now();
    if (now - this.lastQuotes < 4 * 60_000 && this.quotes.size) return;
    const ad = this.deps.adapter();
    const [lin, spot] = await Promise.all([ad.fetchLinearFunding(), ad.fetchSpotPrices()]);
    const held = new Set(this.st?.positions.map((p) => p.symbol) ?? []);
    const uni = lin.filter((x) => spot[x.symbol] > 0).sort((a, b) => b.turnover - a.turnover);
    const pick = [...uni.slice(0, UNIVERSE), ...uni.filter((x) => held.has(x.symbol))];
    const next = new Map<string, CarryQuote>();
    for (const x of pick) {
      let h = this.hist.get(x.symbol);
      if (!h || now - h.at > 4 * 3600_000) {
        try {
          h = { at: now, f: await ad.fetchFunding(x.symbol, now - 9 * 86_400_000) };
          this.hist.set(x.symbol, h);
        } catch (e) {
          this.deps.log(`[carry] funding history ${x.symbol}: ${(e as Error).message.slice(0, 100)}`);
        }
        await new Promise((r) => setTimeout(r, 120));
      }
      next.set(x.symbol, { symbol: x.symbol, spot: spot[x.symbol], perp: x.last, funding: x.funding, nextFundingTime: x.nextFundingTime, turnover: x.turnover, settlements: h?.f ?? [] });
    }
    this.quotes = next;
    this.lastQuotes = now;
  }

  view(): unknown {
    const st = this.st;
    if (!st) return { status: 'loading', error: this.error };
    const now = Date.now();
    const p = st.params;
    const held = new Map(st.positions.map((x) => [x.symbol, x]));
    const rows = [...this.quotes.values()]
      .map((q) => ({
        symbol: q.symbol,
        spot: q.spot,
        perp: q.perp,
        basisBp: (q.perp / q.spot - 1) * 1e4,
        fundingApr: q.funding * 3 * 365,
        apr7: trailingApr(q.settlements, now, p.lookbackDays),
        turnover: q.turnover,
        held: held.has(q.symbol),
      }))
      .sort((a, b) => (isFinite(b.apr7) ? b.apr7 : -9) - (isFinite(a.apr7) ? a.apr7 : -9));
    const positions = st.positions.map((pos) => {
      const q = this.quotes.get(pos.symbol);
      const basis = q ? basisPnl(pos, q.spot, q.perp) : 0;
      return { ...pos, basis, net: pos.funding + basis - pos.fees, apr7: q ? trailingApr(q.settlements, now, p.lookbackDays) : NaN };
    });
    const fs = this.fs;
    const factor = fs
      ? {
          params: fs.params,
          startedAt: fs.startedAt,
          lastRebalance: fs.lastRebalance,
          equity: factorEquity(fs, (s) => this.perp(s)),
          last: fs.last,
          positions: fs.positions.map((x) => {
            const px = this.perp(x.symbol) ?? x.px;
            const q = this.quotes.get(x.symbol);
            return { symbol: x.symbol, qty: x.qty, notional: x.qty * px, funding: x.funding, fees: x.fees, fundingDay: q ? meanFunding(q.settlements, now, fs.params.lookbackDays) : NaN };
          }),
        }
      : null;
    return { status: 'ok', error: this.error, factor, params: p, startedAt: st.startedAt, lastDecisionDay: st.lastDecisionDay, totals: totals(st, this.quotes), positions, closed: st.closed.slice(-50).reverse(), rows: rows.slice(0, 30), equity: st.equity.slice(-24 * 60) };
  }
}
