// Server-side paper trading. Orders are simulated against the live local book of the instrument
// (walking visible depth), SL/TP are evaluated on the server while it runs, funding is charged at the
// exchange funding rollover. No order ever leaves this process.
import { walkBook } from '../../core/paperDepth.js';
import { computeStats, type ClosedTrade, type PaperPosition, type PaperStats } from '../../core/paper.js';

export interface BookView {
  t: number;
  bids: [number, number][];
  asks: [number, number][];
  gate: boolean; // data fresh + synced
  tick: number;
}

export interface PaperConfig {
  takerFee: number; // fraction
  extraSlippageTicks: number;
}

export interface Position extends PaperPosition {
  key: string; // source:symbol
}
export interface Closed extends ClosedTrade {
  key: string;
  delayed?: boolean;
  fillNote?: string;
}

export interface PaperState {
  cfg: PaperConfig;
  open: Position[];
  closed: Closed[];
  seq: number;
  /** periods during which the server could not evaluate stops for an instrument */
  outages: { key: string; t0: number; t1: number | null; reason: string }[];
  fundingSeen: Record<string, number>;
}

export const EMPTY_PAPER: PaperState = { cfg: { takerFee: 0.0005, extraSlippageTicks: 0 }, open: [], closed: [], seq: 1, outages: [], fundingSeen: {} };

export class PaperService {
  state: PaperState;
  private books = new Map<string, BookView>();
  private dirty = false;
  constructor(initial: PaperState | undefined, private save: (s: PaperState) => Promise<void>, private log: (m: string) => void) {
    this.state = initial ? { ...EMPTY_PAPER, ...initial } : structuredClone(EMPTY_PAPER);
    setInterval(() => void this.persist(), 2000);
  }

  private async persist(): Promise<void> {
    if (!this.dirty) return;
    this.dirty = false;
    try {
      await this.save(this.state);
    } catch (e) {
      this.dirty = true;
      this.log(`paper state save failed: ${(e as Error).message}`);
    }
  }

  private fill(key: string, side: 1 | -1, qty: number): { ok: true; price: number; note: string } | { ok: false; reason: string } {
    const b = this.books.get(key);
    if (!b) return { ok: false, reason: 'no live book for this instrument' };
    if (!b.gate) return { ok: false, reason: 'market data not reliable right now (stale / resync / gap)' };
    if (Date.now() - b.t > 10_000) return { ok: false, reason: 'book older than 10 s' };
    const w = walkBook(side === 1 ? b.asks : b.bids, qty);
    if (!w.ok) return { ok: false, reason: w.reason! };
    const slip = this.state.cfg.extraSlippageTicks * b.tick;
    return { ok: true, price: w.avgPrice + side * slip, note: `${w.levelsUsed} level(s), worst ${w.worstPrice}` };
  }

  order(key: string, side: 1 | -1, qty: number, sl?: number, tp?: number): Position {
    if (!(qty > 0)) throw new Error('quantity must be > 0');
    const f = this.fill(key, side, qty);
    if (!f.ok) throw new Error(f.reason);
    const pos: Position = { id: this.state.seq++, key, side, qty, entry: f.price, entryT: Date.now(), sl, tp, fees: f.price * qty * this.state.cfg.takerFee, funding: 0, note: f.note };
    this.state.open.push(pos);
    this.dirty = true;
    return pos;
  }

  close(id: number, reason: ClosedTrade['reason'] = 'manual', delayed = false): Closed {
    const i = this.state.open.findIndex((p) => p.id === id);
    if (i < 0) throw new Error('no such position');
    const p = this.state.open[i];
    const f = this.fill(p.key, (-p.side) as 1 | -1, p.qty);
    if (!f.ok) throw new Error(f.reason);
    this.state.open.splice(i, 1);
    const fees = p.fees + f.price * p.qty * this.state.cfg.takerFee;
    const gross = (f.price - p.entry) * p.side * p.qty;
    const c: Closed = { ...p, fees, exit: f.price, exitT: Date.now(), reason, gross, net: gross - fees - p.funding, delayed, fillNote: f.note };
    this.state.closed.push(c);
    if (this.state.closed.length > 5000) this.state.closed.splice(0, 1000);
    this.dirty = true;
    return c;
  }

  /** Called on every live book update of an instrument. */
  onBook(key: string, b: BookView): void {
    this.books.set(key, b);
    const out = this.state.outages.find((o) => o.key === key && o.t1 === null);
    if (!b.gate) {
      if (!out && this.state.open.some((p) => p.key === key)) {
        this.state.outages.push({ key, t0: Date.now(), t1: null, reason: 'data not reliable' });
        this.dirty = true;
      }
      return;
    }
    let afterOutage = false;
    if (out) {
      out.t1 = Date.now();
      afterOutage = true;
      this.dirty = true;
    }
    if (!b.bids.length || !b.asks.length) return;
    const bid = b.bids[0][0];
    const ask = b.asks[0][0];
    for (const p of [...this.state.open]) {
      if (p.key !== key) continue;
      const px = p.side === 1 ? bid : ask;
      const hitSl = p.sl !== undefined && (p.side === 1 ? px <= p.sl : px >= p.sl);
      const hitTp = p.tp !== undefined && (p.side === 1 ? px >= p.tp : px <= p.tp);
      if (!hitSl && !hitTp) continue;
      try {
        // fills happen at the book available NOW; after an outage this is explicitly flagged as delayed
        this.close(p.id, hitSl ? 'stop' : 'target', afterOutage);
      } catch (e) {
        this.log(`paper ${hitSl ? 'stop' : 'target'} for #${p.id} not filled: ${(e as Error).message}`);
      }
    }
  }

  /** Funding: charged once per funding rollover at the mark price (positive rate: longs pay). */
  onFunding(key: string, rate: number, mark: number, nextFunding: number): void {
    const seen = this.state.fundingSeen[key];
    this.state.fundingSeen[key] = nextFunding;
    if (!seen || nextFunding <= seen) return;
    for (const p of this.state.open) if (p.key === key) p.funding += p.side * p.qty * mark * rate;
    this.dirty = true;
  }

  view(): PaperState & { stats: PaperStats; unrealized: Record<number, number> } {
    const unrealized: Record<number, number> = {};
    for (const p of this.state.open) {
      const b = this.books.get(p.key);
      if (!b || !b.bids.length || !b.asks.length) continue;
      const px = p.side === 1 ? b.bids[0][0] : b.asks[0][0];
      unrealized[p.id] = (px - p.entry) * p.side * p.qty - p.fees - p.funding;
    }
    return { ...this.state, stats: computeStats(this.state.closed), unrealized };
  }

  setConfig(cfg: Partial<PaperConfig>): void {
    this.state.cfg = { ...this.state.cfg, ...cfg };
    this.dirty = true;
  }

  reset(): void {
    this.state = structuredClone(EMPTY_PAPER);
    this.dirty = true;
  }
}
