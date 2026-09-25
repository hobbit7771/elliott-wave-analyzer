// Small numeric utilities used by detectors: rolling windows, percentiles, EWMA.

/** Time-based rolling sum window. Items older than `windowMs` are evicted lazily. */
export class RollingSum {
  private ts: number[] = [];
  private vs: number[] = [];
  private head = 0;
  sum = 0;
  constructor(public windowMs: number) {}
  add(t: number, v: number): void {
    this.ts.push(t);
    this.vs.push(v);
    this.sum += v;
    this.evict(t);
  }
  evict(now: number): void {
    const lim = now - this.windowMs;
    while (this.head < this.ts.length && this.ts[this.head] < lim) {
      this.sum -= this.vs[this.head];
      this.head++;
    }
    if (this.head > 4096 && this.head * 2 > this.ts.length) {
      this.ts = this.ts.slice(this.head);
      this.vs = this.vs.slice(this.head);
      this.head = 0;
    }
    if (this.head === this.ts.length) this.sum = 0; // kill float residue
  }
  get count(): number {
    return this.ts.length - this.head;
  }
  clear(): void {
    this.ts = [];
    this.vs = [];
    this.head = 0;
    this.sum = 0;
  }
}

/**
 * Fixed-capacity sample reservoir (ring) with percentile queries.
 * Deterministic (no random sampling): keeps the latest `cap` samples.
 */
export class RingSamples {
  private buf: Float64Array;
  private n = 0;
  private i = 0;
  private sortedCache: Float64Array | null = null;
  constructor(public cap: number) {
    this.buf = new Float64Array(cap);
  }
  push(v: number): void {
    this.buf[this.i] = v;
    this.i = (this.i + 1) % this.cap;
    if (this.n < this.cap) this.n++;
    this.sortedCache = null;
  }
  get size(): number {
    return this.n;
  }
  percentile(p: number): number {
    if (this.n === 0) return NaN;
    if (!this.sortedCache) {
      this.sortedCache = this.buf.slice(0, this.n).sort();
    }
    return quantileSorted(this.sortedCache, p);
  }
  mean(): number {
    if (!this.n) return NaN;
    let s = 0;
    for (let k = 0; k < this.n; k++) s += this.buf[k];
    return s / this.n;
  }
  clear(): void {
    this.n = 0;
    this.i = 0;
    this.sortedCache = null;
  }
}

export function quantileSorted(sorted: ArrayLike<number>, p: number): number {
  const n = sorted.length;
  if (!n) return NaN;
  const pos = Math.min(1, Math.max(0, p)) * (n - 1);
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

export function percentile(values: number[], p: number): number {
  if (!values.length) return NaN;
  const s = Float64Array.from(values).sort();
  return quantileSorted(s, p);
}

export function median(values: number[]): number {
  return percentile(values, 0.5);
}

export class Ewma {
  value = NaN;
  constructor(public alpha: number) {}
  update(v: number): number {
    this.value = isNaN(this.value) ? v : this.alpha * v + (1 - this.alpha) * this.value;
    return this.value;
  }
}

export const clamp = (v: number, lo: number, hi: number): number => (v < lo ? lo : v > hi ? hi : v);
export const clamp01 = (v: number): number => clamp(v, 0, 1);

/** Logistic squashing used to turn a weighted feature score into a probability-like 0..1. */
export const sigmoid = (x: number): number => 1 / (1 + Math.exp(-x));
