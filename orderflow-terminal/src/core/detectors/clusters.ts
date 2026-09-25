// Liquidity clusters (stacked resting liquidity zones) and liquidity vacuums.
import type { BookSide, LiquidityCluster, Trade } from '../types.js';
import { clamp01, median } from '../stats.js';
import { decimalsOf } from '../precision.js';
import { type DetectorContext, fp, fq } from './context.js';

interface ClusterTrack extends LiquidityCluster {
  initialTotal: number;
  announced: boolean;
  lastStatus: string;
  missing: number;
}

export interface BucketProfile {
  side: BookSide;
  p0: number;      // price of first bucket
  step: number;
  qty: number[];   // ascending price
}

/** Group book levels into zone buckets for each side within ±range of mid. */
export function bucketize(ctx: DetectorContext): { bid: BucketProfile; ask: BucketProfile } | null {
  const { book, cfg } = ctx;
  if (!isFinite(book.bestBidTick) || !isFinite(book.bestAskTick)) return null;
  const step = ctx.zoneStep;
  const mid = (book.bestBid + book.bestAsk) / 2;
  const range = mid * cfg.rangePct;
  const loB = Math.floor((mid - range) / step);
  const hiB = Math.floor((mid + range) / step);
  const n = hiB - loB + 1;
  const bid = new Array<number>(n).fill(0);
  const ask = new Array<number>(n).fill(0);
  const loTick = Math.floor((loB * step) / ctx.tick);
  const hiTick = Math.ceil(((hiB + 1) * step) / ctx.tick);
  book.forEachInRange(loTick, hiTick, (side, tick, q) => {
    const i = Math.floor((tick * ctx.tick) / step + 1e-9) - loB;
    if (i < 0 || i >= n) return;
    (side === 'bid' ? bid : ask)[i] += q;
  });
  const p0 = +(loB * step).toFixed(decimalsOf(step));
  return { bid: { side: 'bid', p0, step, qty: bid }, ask: { side: 'ask', p0, step, qty: ask } };
}

export class ClusterDetector {
  private tracks: ClusterTrack[] = [];
  private seq = 0;
  private lastVacuum: { t: number; lo: number; hi: number; side: BookSide }[] = [];
  vacuums: { side: BookSide; lo: number; hi: number; t: number }[] = [];

  constructor(private ctx: DetectorContext) {}

  reset(): void {
    this.tracks = [];
    this.vacuums = [];
  }

  onTrade(tr: Trade): void {
    for (const c of this.tracks) {
      if (c.status === 'broken' || c.status === 'faded') continue;
      const inside = tr.price >= c.lo && tr.price <= c.hi + c.density * 0; // inclusive zone
      // aggressive sells test bid clusters, aggressive buys test ask clusters
      if (inside && ((c.side === 'bid' && tr.side === -1) || (c.side === 'ask' && tr.side === 1))) {
        c.executed += tr.qty;
      }
      const step = this.ctx.zoneStep;
      if ((c.side === 'bid' && tr.price < c.lo - step) || (c.side === 'ask' && tr.price > c.hi + step)) {
        c.status = 'broken';
        c.label = 'Broken Liquidity';
        c.lastSeen = tr.t;
      }
    }
  }

  scan(now: number): void {
    const prof = bucketize(this.ctx);
    if (!prof) return;
    const cc = this.ctx.cfg.cluster;
    const found: { side: BookSide; lo: number; hi: number; total: number; levels: number }[] = [];
    for (const p of [prof.bid, prof.ask]) {
      const nz = p.qty.filter((q) => q > 0);
      if (nz.length < 10) continue;
      const med = median(nz);
      const elev = cc.elevationMult * med;
      let start = -1;
      let gap = 0;
      let total = 0;
      let lv = 0;
      let lastElev = -1;
      const flush = () => {
        if (start >= 0 && lv >= cc.minBuckets) {
          found.push({ side: p.side, lo: p.p0 + start * p.step, hi: p.p0 + (lastElev + 1) * p.step, total, levels: lv });
        }
        start = -1;
        gap = 0;
        total = 0;
        lv = 0;
      };
      for (let i = 0; i < p.qty.length; i++) {
        if (p.qty[i] >= elev) {
          if (start < 0) start = i;
          total += p.qty[i];
          lv++;
          gap = 0;
          lastElev = i;
        } else if (start >= 0) {
          gap++;
          if (gap > cc.maxGapBuckets) flush();
        }
      }
      flush();
      // vacuum: consecutive thin buckets adjacent to the touch region
      this.scanVacuum(p, med, now);
    }
    const dec = decimalsOf(this.ctx.zoneStep);
    // match found zones to tracks by side + overlap
    for (const t of this.tracks) t.missing++;
    for (const f of found) {
      f.lo = +f.lo.toFixed(dec);
      f.hi = +f.hi.toFixed(dec);
      const tr = this.tracks.find((t) => t.side === f.side && t.status !== 'broken' && t.status !== 'faded' && f.lo <= t.hi && f.hi >= t.lo);
      if (tr) {
        tr.lo = f.lo;
        tr.hi = f.hi;
        tr.total = f.total;
        tr.levels = f.levels;
        tr.density = f.total / Math.max(1, Math.round((f.hi - f.lo) / this.ctx.tick));
        tr.lastSeen = now;
        tr.missing = 0;
      } else {
        this.tracks.push({
          id: `cl-${f.side}-${now}-${this.seq++}`,
          side: f.side,
          lo: f.lo,
          hi: f.hi,
          total: f.total,
          levels: f.levels,
          density: f.total / Math.max(1, Math.round((f.hi - f.lo) / this.ctx.tick)),
          firstSeen: now,
          lastSeen: now,
          executed: 0,
          tests: 0,
          status: 'formed',
          label: f.side === 'bid' ? 'Bid Liquidity Cluster' : 'Ask Liquidity Cluster',
          confidence: 0,
          initialTotal: f.total,
          announced: false,
          lastStatus: 'formed',
          missing: 0,
        });
      }
    }
    const keep: ClusterTrack[] = [];
    for (const t of this.tracks) {
      if (t.status !== 'broken' && t.missing > 3) {
        t.status = 'faded';
      }
      this.classify(t, now);
      if ((t.status === 'broken' || t.status === 'faded') && now - t.lastSeen > 120_000) continue;
      keep.push(t);
    }
    this.tracks = keep.slice(-60);
  }

  private classify(t: ClusterTrack, now: number): void {
    if (t.status !== 'broken' && t.status !== 'faded') {
      if (t.executed > 0) t.tests = Math.max(t.tests, 1);
      if (t.executed >= 0.5 * t.initialTotal && t.total >= 0.5 * t.initialTotal) {
        t.status = 'absorption';
        t.label = 'Absorption Cluster';
      } else if (t.executed > 0) {
        t.status = 'tested';
        t.label = 'Tested Liquidity';
      }
    }
    const age = now - t.firstSeen;
    const cc = this.ctx.cfg.cluster;
    t.confidence = Math.round(100 * clamp01(0.3 + 0.3 * clamp01(age / (6 * cc.minHoldMs)) + 0.2 * clamp01(t.levels / (3 * cc.minBuckets)) + 0.2 * clamp01(t.executed / Math.max(t.initialTotal, 1e-12))));
    if (!t.announced && age >= cc.minHoldMs && t.status !== 'faded' && t.status !== 'broken') {
      t.announced = true;
      t.lastStatus = t.status;
      this.emitCluster(t, now, 'formed');
    } else if (t.announced && t.status !== t.lastStatus && t.status !== 'faded') {
      t.lastStatus = t.status;
      this.emitCluster(t, now, t.status);
    }
  }

  private emitCluster(t: ClusterTrack, now: number, what: string): void {
    if (!this.ctx.gateOpen || t.confidence < this.ctx.cfg.minConfidence) return;
    this.ctx.emit({
      id: t.id,
      t: t.firstSeen,
      endT: what === 'broken' ? now : undefined,
      kind: 'cluster',
      title: t.label,
      side: t.side,
      price: t.lo,
      priceHi: t.hi,
      confidence: t.confidence,
      status: t.status,
      explain: `${t.label}: ${t.levels} elevated ${fp(this.ctx, this.ctx.zoneStep)} buckets ${fp(this.ctx, t.lo)}–${fp(this.ctx, t.hi)} holding ${fq(t.total)} (≥ ${this.ctx.cfg.cluster.elevationMult}× median bucket), ` +
        `age ${((now - t.firstSeen) / 1000).toFixed(0)}s, executed into it ${fq(t.executed)}${what === 'broken' ? '; price traded through' : ''}.`,
      data: { total: t.total, levels: t.levels, executed: t.executed },
    });
  }

  private scanVacuum(p: BucketProfile, med: number, now: number): void {
    const vc = this.ctx.cfg.vacuum;
    const thin = vc.factor * med;
    const { book } = this.ctx;
    const touch = p.side === 'bid' ? book.bestBid : book.bestAsk;
    const touchIdx = Math.floor((touch - p.p0) / p.step + 1e-9);
    // walk away from the touch
    const dir = p.side === 'bid' ? -1 : 1;
    let run = 0;
    let runStart = -1;
    for (let d = 1; d <= vc.maxDistanceBuckets; d++) {
      const i = touchIdx + dir * d;
      if (i < 0 || i >= p.qty.length) break;
      if (p.qty[i] <= thin) {
        if (run === 0) runStart = i;
        run++;
        if (run >= vc.minBuckets) {
          const a = Math.min(runStart, i);
          const b = Math.max(runStart, i);
          const lo = +(p.p0 + a * p.step).toFixed(decimalsOf(p.step));
          const hi = +(p.p0 + (b + 1) * p.step).toFixed(decimalsOf(p.step));
          // extend to the end of the thin run
          let e = i + dir;
          while (e >= 0 && e < p.qty.length && p.qty[e] <= thin) e += dir;
          const lo2 = dir < 0 ? +(p.p0 + (e + 1) * p.step).toFixed(decimalsOf(p.step)) : lo;
          const hi2 = dir > 0 ? +(p.p0 + e * p.step).toFixed(decimalsOf(p.step)) : hi;
          this.reportVacuum(p.side, lo2, hi2, med, now);
          return;
        }
      } else run = 0;
    }
  }

  private reportVacuum(side: BookSide, lo: number, hi: number, med: number, now: number): void {
    this.vacuums = this.vacuums.filter((v) => now - v.t < 5000 && v.side !== side);
    this.vacuums.push({ side, lo, hi, t: now });
    const vc = this.ctx.cfg.vacuum;
    this.lastVacuum = this.lastVacuum.filter((v) => now - v.t < vc.cooldownMs);
    if (this.lastVacuum.some((v) => v.side === side && lo <= v.hi && hi >= v.lo)) return;
    this.lastVacuum.push({ t: now, lo, hi, side });
    const widthBuckets = Math.round((hi - lo) / this.ctx.zoneStep);
    const conf = Math.round(100 * clamp01(0.35 + 0.65 * clamp01(widthBuckets / (4 * vc.minBuckets))));
    if (!this.ctx.gateOpen || conf < this.ctx.cfg.minConfidence) return;
    this.ctx.emit({
      id: `vac-${side}-${now}`,
      t: now,
      kind: 'vacuum',
      title: side === 'bid' ? 'Liquidity Vacuum below' : 'Liquidity Vacuum above',
      side,
      price: lo,
      priceHi: hi,
      confidence: conf,
      explain: `${widthBuckets} consecutive ${fp(this.ctx, this.ctx.zoneStep)} buckets ${fp(this.ctx, lo)}–${fp(this.ctx, hi)} on the ${side} side hold ≤ ${(vc.factor * 100).toFixed(0)}% of the median bucket (${fq(med)}) — price can travel fast through this zone.`,
      data: { width: widthBuckets },
    });
  }

  list(): LiquidityCluster[] {
    return this.tracks
      .filter((t) => t.announced)
      .map(({ initialTotal: _i, announced: _a, lastStatus: _l, missing: _m, ...c }) => c);
  }
}
