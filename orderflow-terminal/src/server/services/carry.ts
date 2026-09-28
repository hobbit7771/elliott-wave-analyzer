// Paper funding-carry strategy (long spot + short perpetual on Bybit), see src/core/carry.ts for the rules.
// Quotes are refreshed every few minutes, funding history of the universe every 4 h; the daily decision and the
// accrual of settled funding run on every update; the state is kept in Supabase (oft.settings 'carry:state').
import type { Repo } from '../persist/repo.js';
import type { BybitLinearAdapter } from '../adapters/bybit.js';
import { SITE_CARRY_PARAMS, accrue, decide, newCarryState, totals, trailingApr, basisPnl, type CarryQuote, type CarryState, type Funding } from '../../core/carry.js';

const KEY = 'carry:state';
const UNIVERSE = 40; // the most liquid USDT perpetuals that also have a spot pair

export class CarryService {
  private st: CarryState | null = null;
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
      this.error = '';
    } catch (e) {
      this.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[carry] update failed: ${this.error}`);
    } finally {
      this.busy = false;
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
    return { status: 'ok', error: this.error, params: p, startedAt: st.startedAt, lastDecisionDay: st.lastDecisionDay, totals: totals(st, this.quotes), positions, closed: st.closed.slice(-50).reverse(), rows: rows.slice(0, 30), equity: st.equity.slice(-24 * 60) };
  }
}
