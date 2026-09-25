// Paper trading & event backtesting. Never sends orders anywhere: pure simulation on real prices.
import type { EventKind, MarketEvent, Trade } from './types.js';

export interface PaperCosts {
  takerFee: number;       // fraction, e.g. 0.0005
  slippageTicks: number;  // added against the trader on market fills and stops
  tick: number;
  /** used by backtests where historic book spread is unknown */
  assumedSpreadTicks: number;
}

export interface PaperPosition {
  id: number;
  side: 1 | -1;
  qty: number;
  entry: number;
  entryT: number;
  sl?: number;
  tp?: number;
  fees: number;
  funding: number;
  note?: string;
}

export interface ClosedTrade extends PaperPosition {
  exit: number;
  exitT: number;
  reason: 'manual' | 'stop' | 'target' | 'time' | 'end';
  gross: number;
  net: number;
}

export interface PaperStats {
  trades: number;
  wins: number;
  winRate: number;
  gross: number;
  fees: number;
  funding: number;
  net: number;
  expectancy: number;
  avgWin: number;
  avgLoss: number;
  profitFactor: number;
  maxDrawdown: number;
  equity: { t: number; v: number }[];
}

export class PaperBook {
  open: PaperPosition[] = [];
  closed: ClosedTrade[] = [];
  private seq = 1;
  constructor(public costs: PaperCosts) {}

  /** Market entry: longs fill at ask + slippage, shorts at bid - slippage. */
  enter(side: 1 | -1, qty: number, bid: number, ask: number, t: number, sl?: number, tp?: number, note?: string): PaperPosition {
    if (!(qty > 0) || !isFinite(bid) || !isFinite(ask)) throw new Error('invalid paper order');
    const slip = this.costs.slippageTicks * this.costs.tick;
    const px = side === 1 ? ask + slip : bid - slip;
    const pos: PaperPosition = { id: this.seq++, side, qty, entry: px, entryT: t, sl, tp, fees: px * qty * this.costs.takerFee, funding: 0, note };
    this.open.push(pos);
    return pos;
  }

  /** Market exit at the opposite touch. */
  exit(id: number, bid: number, ask: number, t: number, reason: ClosedTrade['reason'] = 'manual'): ClosedTrade | null {
    const i = this.open.findIndex((p) => p.id === id);
    if (i < 0) return null;
    const p = this.open[i];
    const slip = this.costs.slippageTicks * this.costs.tick;
    const px = p.side === 1 ? bid - slip : ask + slip;
    return this.close(i, px, t, reason);
  }

  private close(i: number, px: number, t: number, reason: ClosedTrade['reason']): ClosedTrade {
    const p = this.open[i];
    this.open.splice(i, 1);
    const fees = p.fees + px * p.qty * this.costs.takerFee;
    const gross = (px - p.entry) * p.side * p.qty;
    const c: ClosedTrade = { ...p, fees, exit: px, exitT: t, reason, gross, net: gross - fees - p.funding };
    this.closed.push(c);
    return c;
  }

  /** Check stops / targets against the current touch. Stops fill with slippage; targets fill at the limit. */
  mark(bid: number, ask: number, t: number): ClosedTrade[] {
    const out: ClosedTrade[] = [];
    const slip = this.costs.slippageTicks * this.costs.tick;
    for (let i = this.open.length - 1; i >= 0; i--) {
      const p = this.open[i];
      const exitPx = p.side === 1 ? bid : ask;
      if (p.sl !== undefined && (p.side === 1 ? exitPx <= p.sl : exitPx >= p.sl)) {
        out.push(this.close(i, p.side === 1 ? Math.min(exitPx, p.sl) - slip : Math.max(exitPx, p.sl) + slip, t, 'stop'));
      } else if (p.tp !== undefined && (p.side === 1 ? exitPx >= p.tp : exitPx <= p.tp)) {
        out.push(this.close(i, p.tp, t, 'target'));
      }
    }
    return out;
  }

  /** Apply a funding payment (rate as fraction) at mark price. Longs pay positive funding. */
  applyFunding(rate: number, mark: number): void {
    for (const p of this.open) p.funding += p.side * p.qty * mark * rate;
  }

  unrealized(bid: number, ask: number): number {
    return this.open.reduce((s, p) => s + ((p.side === 1 ? bid : ask) - p.entry) * p.side * p.qty - p.fees - p.funding, 0);
  }

  stats(): PaperStats {
    return computeStats(this.closed);
  }

  toJSON(): unknown {
    return { open: this.open, closed: this.closed, seq: this.seq };
  }
  load(s: { open: PaperPosition[]; closed: ClosedTrade[]; seq: number }): void {
    this.open = s.open ?? [];
    this.closed = s.closed ?? [];
    this.seq = s.seq ?? 1;
  }
}

export function computeStats(closed: ClosedTrade[]): PaperStats {
  let eq = 0;
  let peak = 0;
  let mdd = 0;
  const equity: { t: number; v: number }[] = [];
  let wins = 0;
  let sumW = 0;
  let sumL = 0;
  let gross = 0;
  let fees = 0;
  let funding = 0;
  for (const c of [...closed].sort((a, b) => a.exitT - b.exitT)) {
    eq += c.net;
    peak = Math.max(peak, eq);
    mdd = Math.max(mdd, peak - eq);
    equity.push({ t: c.exitT, v: eq });
    if (c.net > 0) {
      wins++;
      sumW += c.net;
    } else sumL += c.net;
    gross += c.gross;
    fees += c.fees;
    funding += c.funding;
  }
  const n = closed.length;
  const losses = n - wins;
  return {
    trades: n,
    wins,
    winRate: n ? wins / n : 0,
    gross,
    fees,
    funding,
    net: eq,
    expectancy: n ? eq / n : 0,
    avgWin: wins ? sumW / wins : 0,
    avgLoss: losses ? sumL / losses : 0,
    profitFactor: sumL < 0 ? sumW / -sumL : sumW > 0 ? Infinity : 0,
    maxDrawdown: mdd,
    equity,
  };
}

export interface BacktestRule {
  kinds: EventKind[];
  minConfidence: number;
  /** follow: long on bid/buy events; fade: the opposite */
  mode: 'follow' | 'fade';
  stopTicks: number;
  targetTicks: number;
  maxHoldMs: number;
  qty: number;
}

export function eventDirection(ev: MarketEvent): 1 | -1 | 0 {
  if (ev.side === 'bid' || ev.side === 'buy') return 1;
  if (ev.side === 'ask' || ev.side === 'sell') return -1;
  return 0;
}

/**
 * Backtest a rule on recorded trades + recorded events. Fills: entry at the first trade after the event
 * ± half the assumed spread + slippage; exits at stop/target (checked on trade prices) or on timeout.
 * One position at a time.
 */
export function backtestEvents(trades: Trade[], events: MarketEvent[], rule: BacktestRule, costs: PaperCosts): { closed: ClosedTrade[]; stats: PaperStats; skipped: number } {
  const book = new PaperBook(costs);
  const evs = events.filter((e) => rule.kinds.includes(e.kind) && e.confidence >= rule.minConfidence && eventDirection(e) !== 0).sort((a, b) => a.t - b.t);
  const half = (costs.assumedSpreadTicks * costs.tick) / 2;
  let ei = 0;
  let skipped = 0;
  let pos: PaperPosition | null = null;
  for (const tr of trades) {
    const bid = tr.price - half;
    const ask = tr.price + half;
    if (pos) {
      const c = book.mark(bid, ask, tr.t);
      if (c.length) pos = null;
      else if (tr.t - pos.entryT >= rule.maxHoldMs) {
        book.exit(pos.id, bid, ask, tr.t, 'time');
        pos = null;
      }
    }
    while (ei < evs.length && evs[ei].t <= tr.t) {
      const ev = evs[ei++];
      if (pos) {
        skipped++;
        continue;
      }
      let dir = eventDirection(ev);
      if (rule.mode === 'fade') dir = (dir * -1) as 1 | -1;
      const d = dir as 1 | -1;
      const ref = d === 1 ? ask : bid;
      pos = book.enter(d, rule.qty, bid, ask, tr.t, ref - d * rule.stopTicks * costs.tick, ref + d * rule.targetTicks * costs.tick, `${ev.kind} ${ev.confidence}`);
    }
  }
  if (pos && trades.length) {
    const last = trades[trades.length - 1];
    book.exit(pos.id, last.price - half, last.price + half, last.t, 'end');
  }
  return { closed: book.closed, stats: book.stats(), skipped };
}
