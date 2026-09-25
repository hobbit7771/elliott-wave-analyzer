// Heatmap column recording from the live local order book + flow records, and downsampling.
import type { HeatColumn, Trade } from './types.js';
import type { FlowRecord, OrderBook } from './orderbook.js';
import { decimalsOf, toTick } from './precision.js';

export interface HeatmapConfig {
  /** bucket size in price units (multiple of tick) */
  step: number;
  /** number of buckets on each side of mid to record */
  halfBuckets: number;
  intervalMs: number;
}

/** Choose a bucket size so that the reliable book range spans roughly `target` buckets. */
export function autoHeatStep(tick: number, bookRange: number, target = 400): number {
  const raw = bookRange / target;
  const mult = Math.max(1, raw / tick);
  const nice = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000];
  let m = nice[nice.length - 1];
  for (const n of nice) if (n >= mult) {
    m = n;
    break;
  }
  return +(m * tick).toFixed(decimalsOf(tick));
}

/**
 * Accumulates flow during an interval and snapshots resting liquidity at interval end.
 * Missing / unreliable book ranges are encoded as -1 so renderers can show "no data" instead of "empty".
 */
export class HeatmapRecorder {
  private exec = new Map<number, [number, number]>();
  private add = new Map<number, number>();
  private rem = new Map<number, number>();
  private hi = 0;
  private lo = 0;
  private last = 0;
  private start = 0;
  constructor(public cfg: HeatmapConfig, public tick: number) {}

  private b(price: number): number {
    return Math.floor(price / this.cfg.step + 1e-9);
  }

  onTrade(tr: Trade): void {
    const k = this.b(tr.price);
    const e = this.exec.get(k) ?? [0, 0];
    if (tr.side === 1) e[0] += tr.qty;
    else e[1] += tr.qty;
    this.exec.set(k, e);
    if (!this.hi || tr.price > this.hi) this.hi = tr.price;
    if (!this.lo || tr.price < this.lo) this.lo = tr.price;
    this.last = tr.price;
  }

  onFlow(recs: FlowRecord[]): void {
    for (const r of recs) {
      const k = Math.floor((r.tick * this.tick) / this.cfg.step + 1e-9);
      if (r.added > 0) this.add.set(k, (this.add.get(k) ?? 0) + r.added);
      if (r.cancelled !== 0) this.rem.set(k, (this.rem.get(k) ?? 0) + r.cancelled);
    }
  }

  /** Close the interval at time t; returns null if the book is not usable. */
  snapshot(t: number, book: OrderBook): HeatColumn | null {
    const bb = book.bestBid;
    const ba = book.bestAsk;
    if (!isFinite(bb) || !isFinite(ba)) {
      this.resetFlow(t);
      return null;
    }
    const step = this.cfg.step;
    const mid = (bb + ba) / 2;
    const midB = this.b(mid);
    const b0 = midB - this.cfg.halfBuckets;
    const n = this.cfg.halfBuckets * 2 + 1;
    const bids = new Array<number>(n).fill(0);
    const asks = new Array<number>(n).fill(0);
    const loTick = toTick(b0 * step, this.tick);
    const hiTick = toTick((b0 + n) * step, this.tick) - 1;
    // unreliable edges -> -1
    const relLoB = isFinite(book.reliableLo) ? Math.floor((book.reliableLo * this.tick) / step + 1e-9) : -Infinity;
    const relHiB = isFinite(book.reliableHi) ? Math.floor((book.reliableHi * this.tick) / step + 1e-9) : Infinity;
    for (let i = 0; i < n; i++) {
      const bi = b0 + i;
      if (bi < relLoB || bi > relHiB) {
        bids[i] = -1;
        asks[i] = -1;
      }
    }
    book.forEachInRange(loTick, hiTick, (side, tick, qty) => {
      const i = Math.floor((tick * this.tick) / step + 1e-9) - b0;
      if (i < 0 || i >= n) return;
      if (side === 'bid') bids[i] = Math.max(0, bids[i]) + qty;
      else asks[i] = Math.max(0, asks[i]) + qty;
    });
    const dec = decimalsOf(step) + 2;
    const r = (v: number) => +v.toFixed(dec);
    const col: HeatColumn = {
      t,
      dt: this.start ? t - this.start : this.cfg.intervalMs,
      p0: +(b0 * step).toFixed(decimalsOf(step)),
      step,
      n,
      bids: bids.map(r),
      asks: asks.map(r),
      exec: [...this.exec].filter(([k]) => k >= b0 && k < b0 + n).map(([k, e]) => [k - b0, r(e[0]), r(e[1])]),
      add: [...this.add].filter(([k]) => k >= b0 && k < b0 + n).map(([k, q]) => [k - b0, r(q)]),
      rem: [...this.rem].filter(([k, q]) => k >= b0 && k < b0 + n && q > 0).map(([k, q]) => [k - b0, r(q)]),
      bb,
      ba,
      hi: this.hi,
      lo: this.lo,
      last: this.last || mid,
    };
    this.resetFlow(t);
    return col;
  }

  private resetFlow(t: number): void {
    this.exec.clear();
    this.add.clear();
    this.rem.clear();
    this.hi = 0;
    this.lo = 0;
    this.start = t;
  }
}

/**
 * Merge consecutive columns into one (for retention tiers / zoomed-out rendering).
 * Resting liquidity = time-weighted average over the merged columns (unknown = -1 only if unknown in all),
 * flows are summed, price extremes merged.
 */
export function mergeColumns(cols: HeatColumn[]): HeatColumn {
  if (cols.length === 1) return cols[0];
  const step = cols[0].step;
  let lo = Infinity;
  let hi = -Infinity;
  for (const c of cols) {
    const b0 = Math.round(c.p0 / step);
    lo = Math.min(lo, b0);
    hi = Math.max(hi, b0 + c.n - 1);
  }
  const n = hi - lo + 1;
  const bs = new Float64Array(n);
  const as = new Float64Array(n);
  const w = new Float64Array(n);
  const exec = new Map<number, [number, number]>();
  const add = new Map<number, number>();
  const rem = new Map<number, number>();
  let tw = 0;
  let phi = 0;
  let plo = 0;
  for (const c of cols) {
    const off = Math.round(c.p0 / step) - lo;
    const dt = c.dt || 1;
    tw += dt;
    for (let i = 0; i < c.n; i++) {
      if (c.bids[i] < 0) continue;
      bs[off + i] += c.bids[i] * dt;
      as[off + i] += c.asks[i] * dt;
      w[off + i] += dt;
    }
    for (const [i, b, s] of c.exec) {
      const e = exec.get(off + i) ?? [0, 0];
      e[0] += b;
      e[1] += s;
      exec.set(off + i, e);
    }
    for (const [i, q] of c.add) add.set(off + i, (add.get(off + i) ?? 0) + q);
    for (const [i, q] of c.rem) rem.set(off + i, (rem.get(off + i) ?? 0) + q);
    if (c.hi && (!phi || c.hi > phi)) phi = c.hi;
    if (c.lo && (!plo || c.lo < plo)) plo = c.lo;
  }
  const dec = decimalsOf(step) + 2;
  const r = (v: number) => +v.toFixed(dec);
  const bids: number[] = [];
  const asks: number[] = [];
  for (let i = 0; i < n; i++) {
    // average only over the time the bucket was observed, but only if observed >= 25% of the span
    if (w[i] === 0 || w[i] < tw * 0.25) {
      bids.push(w[i] === 0 ? -1 : r(bs[i] / w[i]));
      asks.push(w[i] === 0 ? -1 : r(as[i] / w[i]));
    } else {
      bids.push(r(bs[i] / w[i]));
      asks.push(r(as[i] / w[i]));
    }
  }
  const last = cols[cols.length - 1];
  return {
    t: last.t,
    dt: tw,
    p0: +(lo * step).toFixed(decimalsOf(step)),
    step,
    n,
    bids,
    asks,
    exec: [...exec].map(([k, e]) => [k, r(e[0]), r(e[1])]),
    add: [...add].map(([k, q]) => [k, r(q)]),
    rem: [...rem].map(([k, q]) => [k, r(q)]),
    bb: last.bb,
    ba: last.ba,
    hi: phi,
    lo: plo,
    last: last.last,
  };
}

/** Downsample a column series so that each output column covers `bucketMs`. */
export function downsample(cols: HeatColumn[], bucketMs: number): HeatColumn[] {
  const out: HeatColumn[] = [];
  let group: HeatColumn[] = [];
  let gk = NaN;
  for (const c of cols) {
    const k = Math.floor((c.t - 1) / bucketMs);
    if (k !== gk && group.length) {
      out.push(mergeColumns(group));
      group = [];
    }
    gk = k;
    group.push(c);
  }
  if (group.length) out.push(mergeColumns(group));
  return out;
}

/** Flatten columns to CSV rows for export. */
export function columnsToCsv(cols: HeatColumn[]): string {
  const lines = ['time_utc,price,bid_qty,ask_qty,buy_exec,sell_exec,added,cancelled'];
  for (const c of cols) {
    const ex = new Map(c.exec.map(([i, b, s]) => [i, [b, s]]));
    const ad = new Map(c.add);
    const rm = new Map(c.rem);
    const iso = new Date(c.t).toISOString();
    const dec = decimalsOf(c.step);
    for (let i = 0; i < c.n; i++) {
      const b = c.bids[i];
      const a = c.asks[i];
      const e = ex.get(i);
      if (b <= 0 && a <= 0 && !e && !ad.has(i) && !rm.has(i)) continue;
      lines.push([iso, (c.p0 + i * c.step).toFixed(dec), b < 0 ? '' : b, a < 0 ? '' : a, e ? e[0] : 0, e ? e[1] : 0, ad.get(i) ?? 0, rm.get(i) ?? 0].join(','));
    }
  }
  return lines.join('\n');
}
