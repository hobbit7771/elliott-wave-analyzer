// Local order-book reconstruction, sequence validation and add/cancel/execute classification.
import type { BookSide, BookSnapshot, BookStats, DepthDiff, Level, Trade } from './types.js';
import { decimalsOf, fromTick, toTick } from './precision.js';

export interface LevelChange {
  side: BookSide;
  tick: number;
  oldQty: number;
  newQty: number;
}

/**
 * Price-level book keyed by integer tick index.
 * `reliableLo/Hi` bound the tick range covered by the last REST snapshot: outside it,
 * levels are only known once they change, so consumers (heatmap, detectors) must not
 * treat missing levels there as "empty".
 */
export class OrderBook {
  readonly bids = new Map<number, number>();
  readonly asks = new Map<number, number>();
  readonly dec: number;
  bestBidTick = -Infinity;
  bestAskTick = Infinity;
  reliableLo = -Infinity;
  reliableHi = Infinity;
  lastUpdateId = 0;
  lastT = 0;

  constructor(public readonly tick: number) {
    this.dec = decimalsOf(tick);
  }

  clear(): void {
    this.bids.clear();
    this.asks.clear();
    this.bestBidTick = -Infinity;
    this.bestAskTick = Infinity;
    this.reliableLo = -Infinity;
    this.reliableHi = Infinity;
    this.lastUpdateId = 0;
  }

  applySnapshot(s: BookSnapshot): void {
    this.clear();
    let lo = Infinity;
    let hi = -Infinity;
    for (const [p, q] of s.bids) {
      if (q <= 0) continue;
      const k = toTick(p, this.tick);
      this.bids.set(k, q);
      if (k > this.bestBidTick) this.bestBidTick = k;
      if (k < lo) lo = k;
    }
    for (const [p, q] of s.asks) {
      if (q <= 0) continue;
      const k = toTick(p, this.tick);
      this.asks.set(k, q);
      if (k < this.bestAskTick) this.bestAskTick = k;
      if (k > hi) hi = k;
    }
    this.reliableLo = lo;
    this.reliableHi = hi;
    this.lastUpdateId = s.lastUpdateId;
    this.lastT = s.t;
  }

  /** Apply a diff (already sequence-validated). Returns the level changes. */
  applyDiff(d: DepthDiff): LevelChange[] {
    const changes: LevelChange[] = [];
    this.applySide('bid', d.bids, changes);
    this.applySide('ask', d.asks, changes);
    this.lastUpdateId = d.lastId;
    this.lastT = d.t;
    this.fixCrossed();
    return changes;
  }

  private applySide(side: BookSide, levels: Level[], out: LevelChange[]): void {
    const map = side === 'bid' ? this.bids : this.asks;
    let needBest = false;
    for (const [p, q] of levels) {
      const k = toTick(p, this.tick);
      const old = map.get(k) ?? 0;
      if (q <= 0) {
        if (old > 0) {
          map.delete(k);
          out.push({ side, tick: k, oldQty: old, newQty: 0 });
          if ((side === 'bid' && k === this.bestBidTick) || (side === 'ask' && k === this.bestAskTick)) needBest = true;
        }
      } else {
        if (old !== q) out.push({ side, tick: k, oldQty: old, newQty: q });
        map.set(k, q);
        if (side === 'bid' && k > this.bestBidTick) this.bestBidTick = k;
        if (side === 'ask' && k < this.bestAskTick) this.bestAskTick = k;
      }
    }
    if (needBest) this.recomputeBest(side);
  }

  private recomputeBest(side: BookSide): void {
    if (side === 'bid') {
      let b = -Infinity;
      for (const k of this.bids.keys()) if (k > b) b = k;
      this.bestBidTick = b;
    } else {
      let a = Infinity;
      for (const k of this.asks.keys()) if (k < a) a = k;
      this.bestAskTick = a;
    }
  }

  /** A crossed book can only happen from stale levels; remove the stale side. */
  private fixCrossed(): void {
    let guard = 0;
    while (this.bestBidTick >= this.bestAskTick && isFinite(this.bestBidTick) && isFinite(this.bestAskTick) && guard++ < 1000) {
      // Keep the side updated most recently is unknowable per level; drop the smaller-quantity crossing level.
      const bq = this.bids.get(this.bestBidTick) ?? 0;
      const aq = this.asks.get(this.bestAskTick) ?? 0;
      if (bq <= aq) {
        this.bids.delete(this.bestBidTick);
        this.recomputeBest('bid');
      } else {
        this.asks.delete(this.bestAskTick);
        this.recomputeBest('ask');
      }
    }
  }

  qtyAt(side: BookSide, tick: number): number {
    return (side === 'bid' ? this.bids : this.asks).get(tick) ?? 0;
  }

  get bestBid(): number {
    return isFinite(this.bestBidTick) ? fromTick(this.bestBidTick, this.tick, this.dec) : NaN;
  }
  get bestAsk(): number {
    return isFinite(this.bestAskTick) ? fromTick(this.bestAskTick, this.tick, this.dec) : NaN;
  }
  get size(): number {
    return this.bids.size + this.asks.size;
  }

  /** Top `n` levels per side, best first, as [price, qty]. */
  top(n: number): { bids: Level[]; asks: Level[] } {
    const bk = [...this.bids.keys()].sort((a, b) => b - a).slice(0, n);
    const ak = [...this.asks.keys()].sort((a, b) => a - b).slice(0, n);
    return {
      bids: bk.map((k) => [fromTick(k, this.tick, this.dec), this.bids.get(k)!] as Level),
      asks: ak.map((k) => [fromTick(k, this.tick, this.dec), this.asks.get(k)!] as Level),
    };
  }

  /** Levels within [loTick, hiTick] clipped to the reliable range. */
  forEachInRange(loTick: number, hiTick: number, fn: (side: BookSide, tick: number, qty: number) => void): void {
    const lo = Math.max(loTick, this.reliableLo);
    const hi = Math.min(hiTick, this.reliableHi);
    for (const [k, q] of this.bids) if (k >= lo && k <= hi) fn('bid', k, q);
    for (const [k, q] of this.asks) if (k >= lo && k <= hi) fn('ask', k, q);
  }

  /** Drop levels further than `ticks` from the touch to bound memory. */
  prune(ticks: number): number {
    if (!isFinite(this.bestBidTick) || !isFinite(this.bestAskTick)) return 0;
    let removed = 0;
    const lo = this.bestBidTick - ticks;
    const hi = this.bestAskTick + ticks;
    for (const k of this.bids.keys()) if (k < lo) (this.bids.delete(k), removed++);
    for (const k of this.asks.keys()) if (k > hi) (this.asks.delete(k), removed++);
    if (this.reliableLo < lo) this.reliableLo = lo;
    if (this.reliableHi > hi) this.reliableHi = hi;
    return removed;
  }

  stats(topN: number, ofi: number, t: number): BookStats {
    const bb = this.bestBid;
    const ba = this.bestAsk;
    const bq = this.bids.get(this.bestBidTick) ?? 0;
    const aq = this.asks.get(this.bestAskTick) ?? 0;
    let sb = 0;
    let sa = 0;
    for (let i = 0, k = this.bestBidTick; i < topN * 4 && isFinite(k); i++, k--) {
      const q = this.bids.get(k);
      if (q !== undefined) sb += q;
      if (this.bestBidTick - k >= topN) break;
    }
    for (let i = 0, k = this.bestAskTick; i < topN * 4 && isFinite(k); i++, k++) {
      const q = this.asks.get(k);
      if (q !== undefined) sa += q;
      if (k - this.bestAskTick >= topN) break;
    }
    const mid = (bb + ba) / 2;
    const micro = bq + aq > 0 ? (bb * aq + ba * bq) / (bq + aq) : mid;
    return {
      t,
      bestBid: bb,
      bestAsk: ba,
      bidQty: bq,
      askQty: aq,
      spread: ba - bb,
      mid,
      microprice: micro,
      obi: sb + sa > 0 ? (sb - sa) / (sb + sa) : 0,
      ofi,
      depthLevels: this.size,
    };
  }
}

export type SyncMode = 'futures' | 'spot';
export type SyncResult = { applied: DepthDiff[]; gap: boolean; dropped: number; reason?: string };

/**
 * Implements exchange diff-depth synchronization rules:
 *  futures: first diff must have U <= lastUpdateId <= u; afterwards pu == previous u.
 *  spot:    first diff must have U <= lastUpdateId+1 <= u; afterwards U == previous u + 1.
 * Diffs received before the snapshot are buffered (bounded).
 */
export class BookSync {
  state: 'idle' | 'buffering' | 'synced' = 'idle';
  private buffer: DepthDiff[] = [];
  private lastU = 0;
  /** snapshot applied but no buffered diff reached it yet: the next live diff must bridge the snapshot id */
  private awaitingBridge = false;
  private snapId = 0;
  dropped = 0;

  constructor(public mode: SyncMode, public maxBuffer = 2000) {}

  reset(): void {
    this.state = 'buffering';
    this.buffer = [];
    this.lastU = 0;
    this.awaitingBridge = false;
  }

  get buffered(): number {
    return this.buffer.length;
  }

  /** Feed a diff. Returns diffs that may be applied to the book in order. */
  onDiff(d: DepthDiff): SyncResult {
    if (this.state === 'idle') this.state = 'buffering';
    if (this.state === 'buffering') {
      this.buffer.push(d);
      if (this.buffer.length > this.maxBuffer) {
        this.buffer.shift();
        this.dropped++;
      }
      return { applied: [], gap: false, dropped: 0 };
    }
    if (this.awaitingBridge) {
      const L = this.snapId;
      const stale = this.mode === 'futures' ? d.lastId < L : d.lastId <= L;
      if (stale) {
        this.dropped++;
        return { applied: [], gap: false, dropped: 1 };
      }
      const bridges = this.mode === 'futures' ? d.firstId <= L && d.lastId >= L : d.firstId <= L + 1 && d.lastId >= L + 1;
      if (!bridges) {
        this.awaitingBridge = false;
        this.state = 'buffering';
        this.buffer = [d];
        return { applied: [], gap: true, dropped: 0, reason: `snapshot ${L} not bridged by live diff U=${d.firstId} u=${d.lastId}` };
      }
      this.awaitingBridge = false;
      this.lastU = d.lastId;
      return { applied: [d], gap: false, dropped: 0 };
    }
    if (!this.continues(d)) {
      this.state = 'buffering';
      this.buffer = [d];
      return { applied: [], gap: true, dropped: 0, reason: this.gapReason(d) };
    }
    this.lastU = d.lastId;
    return { applied: [d], gap: false, dropped: 0 };
  }

  private continues(d: DepthDiff): boolean {
    if (this.mode === 'futures') return d.prevLastId === this.lastU;
    return d.firstId === this.lastU + 1;
  }

  private gapReason(d: DepthDiff): string {
    return this.mode === 'futures'
      ? `sequence gap: pu=${d.prevLastId} expected ${this.lastU}`
      : `sequence gap: U=${d.firstId} expected ${this.lastU + 1}`;
  }

  /**
   * Called with a fresh REST snapshot. Returns the buffered diffs to apply after the
   * snapshot, or gap=true if the buffer cannot bridge the snapshot (need a new snapshot).
   */
  onSnapshot(lastUpdateId: number): SyncResult {
    let dropped = 0;
    const pending = this.buffer;
    this.buffer = [];
    const out: DepthDiff[] = [];
    let first = true;
    let lastU = lastUpdateId;
    for (const d of pending) {
      if (first) {
        if (this.mode === 'futures') {
          if (d.lastId < lastUpdateId) {
            dropped++;
            continue;
          }
          if (!(d.firstId <= lastUpdateId && d.lastId >= lastUpdateId)) {
            this.state = 'buffering';
            return { applied: [], gap: true, dropped, reason: `snapshot ${lastUpdateId} not bridged by U=${d.firstId} u=${d.lastId}` };
          }
        } else {
          if (d.lastId <= lastUpdateId) {
            dropped++;
            continue;
          }
          if (!(d.firstId <= lastUpdateId + 1 && d.lastId >= lastUpdateId + 1)) {
            this.state = 'buffering';
            return { applied: [], gap: true, dropped, reason: `snapshot ${lastUpdateId} not bridged by U=${d.firstId} u=${d.lastId}` };
          }
        }
        first = false;
        out.push(d);
        lastU = d.lastId;
        continue;
      }
      const ok = this.mode === 'futures' ? d.prevLastId === lastU : d.firstId === lastU + 1;
      if (!ok) {
        this.state = 'buffering';
        return { applied: [], gap: true, dropped, reason: 'gap inside buffered diffs' };
      }
      out.push(d);
      lastU = d.lastId;
    }
    this.dropped += dropped;
    this.lastU = lastU;
    this.state = 'synced';
    // snapshot newer than everything buffered: wait for the live diff that contains the snapshot id
    this.awaitingBridge = first;
    this.snapId = lastUpdateId;
    return { applied: out, gap: false, dropped };
  }
}

/** Per-level flow record produced by the classifier. Quantities are in base units. */
export interface FlowRecord {
  t: number;
  side: BookSide;
  tick: number;
  added: number;
  cancelled: number;
  executed: number;
  /** traded volume at the level not explained by visible depletion (refill / hidden liquidity) */
  hidden: number;
  /** displayed quantity after the update */
  qty: number;
  prevQty: number;
}

interface PendingExec {
  qty: number;
  lastT: number;
}
interface RecentLevel {
  t: number;
  cancelled: number;
}

/**
 * Classifies book changes into add / cancel / execute by joining the trade stream with depth diffs.
 *
 * Trades are passive-side attributed: an aggressive sell (side=-1) executes against the bid at
 * the trade price, an aggressive buy against the ask. When a diff at time T arrives, trades with
 * t <= T at that level explain the visible depletion first; the remainder of the depletion is a
 * cancel; executed volume beyond the depletion is "hidden" (level got refilled / hidden size).
 * Late trades (arriving after the diff that already reflected them) re-classify a recent cancel.
 */
export class FlowClassifier {
  private pending = new Map<string, PendingExec>();
  private recent = new Map<string, RecentLevel>();
  private lastDiffT = 0;
  constructor(public readonly tick: number, public lateGraceMs = 1500) {}

  private key(side: BookSide, tick: number): string {
    return side === 'bid' ? 'b' + tick : 'a' + tick;
  }

  reset(): void {
    this.pending.clear();
    this.recent.clear();
    this.lastDiffT = 0;
  }

  /** Register a trade. May return correction records for a late trade. */
  onTrade(tr: Trade, book: OrderBook): FlowRecord[] {
    const side: BookSide = tr.side === -1 ? 'bid' : 'ask';
    const tick = toTick(tr.price, this.tick);
    const k = this.key(side, tick);
    const r = this.recent.get(k);
    // Late trade: the diff covering it was already applied. A trade stamped exactly at the last diff time
    // is ambiguous (may be in the next diff) and is only treated as late if that diff showed a cancel here.
    const late = this.lastDiffT > 0 && (tr.t < this.lastDiffT || (tr.t === this.lastDiffT && !!r && r.t === tr.t && r.cancelled > 0));
    if (late) {
      const qty = book.qtyAt(side, tick);
      if (r && r.cancelled > 0 && this.lastDiffT - r.t <= this.lateGraceMs) {
        const x = Math.min(tr.qty, r.cancelled);
        r.cancelled -= x;
        const rest = tr.qty - x;
        return [{ t: tr.t, side, tick, added: 0, cancelled: -x, executed: x, hidden: rest, qty, prevQty: qty }];
      }
      return [{ t: tr.t, side, tick, added: 0, cancelled: 0, executed: 0, hidden: tr.qty, qty, prevQty: qty }];
    }
    const p = this.pending.get(k);
    if (p) {
      p.qty += tr.qty;
      p.lastT = tr.t;
    } else this.pending.set(k, { qty: tr.qty, lastT: tr.t });
    return [];
  }

  /** Classify the level changes of a diff at time `t`. */
  onDiff(t: number, changes: LevelChange[]): FlowRecord[] {
    const out: FlowRecord[] = [];
    const seen = new Set<string>();
    for (const c of changes) {
      const k = this.key(c.side, c.tick);
      seen.add(k);
      const p = this.pending.get(k);
      const v = p && p.lastT <= t ? p.qty : 0;
      if (p && p.lastT <= t) this.pending.delete(k);
      const drop = Math.max(0, c.oldQty - c.newQty);
      const executed = Math.min(drop, v);
      const cancelled = drop - executed;
      const added = Math.max(0, c.newQty - c.oldQty);
      const hidden = v - executed;
      out.push({ t, side: c.side, tick: c.tick, added, cancelled, executed, hidden, qty: c.newQty, prevQty: c.oldQty });
      if (cancelled > 0) this.recent.set(k, { t, cancelled });
      else this.recent.delete(k);
    }
    // trades at levels that did not change: fully refilled (or not yet reported: keep if newer than t)
    for (const [k, p] of this.pending) {
      if (seen.has(k) || p.lastT > t) continue;
      const side: BookSide = k[0] === 'b' ? 'bid' : 'ask';
      const tick = +k.slice(1);
      out.push({ t, side, tick, added: 0, cancelled: 0, executed: 0, hidden: p.qty, qty: NaN, prevQty: NaN });
      this.pending.delete(k);
    }
    this.lastDiffT = t;
    if (this.recent.size > 5000) {
      for (const [k, r] of this.recent) if (t - r.t > this.lateGraceMs) this.recent.delete(k);
    }
    return out;
  }
}

/** Order-flow imbalance at the touch (Cont, Kukanov & Stoikov 2014). */
export class OfiTracker {
  private pb = NaN;
  private qb = 0;
  private pa = NaN;
  private qa = 0;
  private ts: number[] = [];
  private es: number[] = [];
  sum = 0;
  constructor(public windowMs = 10_000) {}
  update(t: number, bb: number, bq: number, ba: number, aq: number): number {
    if (!isNaN(this.pb)) {
      let e = 0;
      if (bb >= this.pb) e += bq;
      if (bb <= this.pb) e -= this.qb;
      if (ba <= this.pa) e -= aq;
      if (ba >= this.pa) e += this.qa;
      if (e !== 0) {
        this.ts.push(t);
        this.es.push(e);
        this.sum += e;
      }
    }
    this.pb = bb;
    this.qb = bq;
    this.pa = ba;
    this.qa = aq;
    const lim = t - this.windowMs;
    let i = 0;
    while (i < this.ts.length && this.ts[i] < lim) this.sum -= this.es[i++];
    if (i) {
      this.ts.splice(0, i);
      this.es.splice(0, i);
    }
    if (!this.ts.length) this.sum = 0;
    return this.sum;
  }
  reset(): void {
    this.pb = NaN;
    this.pa = NaN;
    this.ts = [];
    this.es = [];
    this.sum = 0;
  }
}
