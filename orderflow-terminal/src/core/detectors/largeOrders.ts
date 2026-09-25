// Large resting limit orders (dynamic threshold), liquidity pulled, spoofing suspicion.
import type { BookSide, LargeOrder, Trade } from '../types.js';
import type { FlowRecord } from '../orderbook.js';
import { RingSamples, clamp, clamp01 } from '../stats.js';
import { toTick } from '../precision.js';
import { type DetectorContext, fp, fq, priceOf } from './context.js';

interface Track {
  side: BookSide;
  tick: number;
  firstSeen: number;
  lastSeen: number;
  peak: number;
  size: number;
  executed: number;
  cancelled: number;
  added: number;
  replenishments: number;
  depletedSinceAdd: number;
  distAtSeen: number;   // distance to mid in ticks when first seen
  confirmed: boolean;
  status: LargeOrder['status'];
  endT: number;
  threshold: number;
  confidence: number;
}

export interface ThresholdInfo {
  bid: number;
  ask: number;
  pctDepth: { bid: number; ask: number };
  pctl: number;
  atrFactor: number;
  samples: number;
  warm: boolean;
}

export class LargeOrderDetector {
  private samples: RingSamples;
  private tracks = new Map<string, Track>();
  /** per level: timestamps of large appear+cancel cycles (flicker) */
  private flicker = new Map<string, number[]>();
  /** levels recently flagged as spoof suspects (used by the iceberg filter) */
  readonly spoofed = new Map<string, number>();
  thr: ThresholdInfo = { bid: Infinity, ask: Infinity, pctDepth: { bid: 0, ask: 0 }, pctl: NaN, atrFactor: 1, samples: 0, warm: false };
  private lastSample = 0;

  constructor(private ctx: DetectorContext) {
    this.samples = new RingSamples(20_000);
  }

  key(side: BookSide, tick: number): string {
    return side[0] + tick;
  }

  reset(): void {
    this.tracks.clear();
  }

  /** Recompute thresholds and scan book (call ~2Hz). */
  scan(now: number): void {
    const { book, cfg } = this.ctx;
    const bbT = book.bestBidTick;
    const baT = book.bestAskTick;
    if (!isFinite(bbT) || !isFinite(baT)) return;
    const mid = (book.bestBid + book.bestAsk) / 2;
    const rangeTicks = Math.max(10, Math.round((mid * cfg.rangePct) / this.ctx.tick));
    let depthB = 0;
    let depthA = 0;
    const levels: [BookSide, number, number][] = [];
    book.forEachInRange(bbT - rangeTicks, baT + rangeTicks, (side, tick, qty) => {
      if (side === 'bid') depthB += qty;
      else depthA += qty;
      levels.push([side, tick, qty]);
    });
    // sample level sizes once per second (bounded ring, deterministic)
    if (now - this.lastSample >= 1000) {
      this.lastSample = now;
      for (const [, , q] of levels) this.samples.push(q);
    }
    const lc = cfg.large;
    const pctl = this.samples.percentile(lc.percentile);
    let atrFactor = 1;
    if (lc.atrAdjust && isFinite(this.ctx.atr1m) && isFinite(this.ctx.atrAvg) && this.ctx.atrAvg > 0) {
      atrFactor = clamp(Math.sqrt(this.ctx.atr1m / this.ctx.atrAvg), 0.75, 1.5);
    }
    const warm = this.samples.size >= lc.minSamples;
    const thrB = Math.max(lc.minQty, lc.depthPct * depthB, isNaN(pctl) ? 0 : pctl) * atrFactor;
    const thrA = Math.max(lc.minQty, lc.depthPct * depthA, isNaN(pctl) ? 0 : pctl) * atrFactor;
    this.thr = { bid: warm ? thrB : Infinity, ask: warm ? thrA : Infinity, pctDepth: { bid: lc.depthPct * depthB, ask: lc.depthPct * depthA }, pctl, atrFactor, samples: this.samples.size, warm };
    if (!warm) return;

    const midTick = (bbT + baT) / 2;
    for (const [side, tick, qty] of levels) {
      const thr = side === 'bid' ? thrB : thrA;
      const dist = Math.abs(tick - midTick);
      const k = this.key(side, tick);
      let tr = this.tracks.get(k);
      if (qty > thr && dist >= lc.minDistanceTicks) {
        if (!tr || tr.status !== 'active' && tr.status !== 'partially_filled') {
          tr = { side, tick, firstSeen: now, lastSeen: now, peak: qty, size: qty, executed: 0, cancelled: 0, added: 0, replenishments: 0, depletedSinceAdd: 0, distAtSeen: dist, confirmed: false, status: 'active', endT: 0, threshold: thr, confidence: 0 };
          this.tracks.set(k, tr);
        }
        tr.size = qty;
        tr.lastSeen = now;
        tr.threshold = thr;
        if (qty > tr.peak) tr.peak = qty;
        if (!tr.confirmed && now - tr.firstSeen >= lc.minHoldMs) {
          tr.confirmed = true;
          tr.confidence = this.confidence(tr, now);
          if (tr.confidence >= this.ctx.cfg.minConfidence) {
            const p = priceOf(this.ctx, tick);
            this.ctx.emit({
              id: `large-${k}-${tr.firstSeen}`,
              t: now,
              kind: 'large_order',
              title: 'Large Limit Order',
              side,
              price: p,
              confidence: tr.confidence,
              explain: `${fq(qty)} resting on the ${side} at ${fp(this.ctx, p)} for ${((now - tr.firstSeen) / 1000).toFixed(1)}s. ` +
                `Threshold ${fq(thr)} = max(${(lc.depthPct * 100).toFixed(1)}% of ${side} depth within ±${(cfg.rangePct * 100).toFixed(1)}% [${fq(side === 'bid' ? lc.depthPct * depthB : lc.depthPct * depthA)}], ` +
                `P${(lc.percentile * 100).toFixed(0)} of level sizes [${fq(pctl)}], min ${fq(lc.minQty)}) × ATR factor ${atrFactor.toFixed(2)}.`,
              data: { size: qty, threshold: thr, distanceTicks: dist },
              status: 'active',
            });
          }
        }
      }
    }
    // tracks whose level dropped below threshold (not reported by flow, e.g. pruned)
    for (const [k, tr] of this.tracks) {
      if (tr.status !== 'active' && tr.status !== 'partially_filled') {
        if (now - tr.endT > 60_000) this.tracks.delete(k);
        continue;
      }
      const q = book.qtyAt(tr.side, tr.tick);
      if (q < tr.threshold * 0.25) this.finish(tr, now, q);
    }
    if (this.spoofed.size > 500) for (const [k, t] of this.spoofed) if (now - t > 600_000) this.spoofed.delete(k);
  }

  onFlow(recs: FlowRecord[], now: number): void {
    for (const r of recs) {
      const tr = this.tracks.get(this.key(r.side, r.tick));
      if (!tr || (tr.status !== 'active' && tr.status !== 'partially_filled')) continue;
      tr.executed += r.executed + r.hidden;
      tr.cancelled += Math.max(0, r.cancelled);
      if (r.cancelled < 0) tr.cancelled = Math.max(0, tr.cancelled + r.cancelled);
      tr.depletedSinceAdd += r.executed + r.hidden;
      if (r.hidden > 0 && r.hidden >= 0.05 * tr.peak) tr.replenishments++;
      if (r.added > 0) {
        if (tr.depletedSinceAdd >= 0.2 * tr.peak) tr.replenishments++;
        tr.depletedSinceAdd = 0;
        tr.added += r.added;
      }
      if (!isNaN(r.qty)) {
        tr.size = r.qty;
        if (r.qty > tr.peak) tr.peak = r.qty;
        if (tr.executed > 0 && tr.status === 'active') tr.status = 'partially_filled';
        if (r.qty < tr.threshold * 0.25) this.finish(tr, now, r.qty);
      }
    }
  }

  onTrade(tr: Trade): void {
    // a trade strictly through a tracked level breaks it
    const t = toTick(tr.price, this.ctx.tick);
    for (const x of this.tracks.values()) {
      if (x.status !== 'active' && x.status !== 'partially_filled') continue;
      if ((x.side === 'bid' && t < x.tick) || (x.side === 'ask' && t > x.tick)) {
        x.status = 'broken';
        x.endT = tr.t;
        if (x.confirmed) this.update(x, tr.t, `Price traded through the level (${fp(this.ctx, tr.price)}).`);
      }
    }
  }

  private finish(tr: Track, now: number, remaining: number): void {
    const removed = tr.executed + tr.cancelled;
    const execFrac = removed > 0 ? tr.executed / removed : 0;
    tr.size = remaining;
    tr.endT = now;
    const k = this.key(tr.side, tr.tick);
    if (execFrac >= 0.5) {
      tr.status = 'filled';
      if (tr.confirmed) this.update(tr, now, `Level consumed by aggressive trades (${fq(tr.executed)} executed).`);
      return;
    }
    tr.status = 'pulled';
    const life = now - tr.firstSeen;
    const p = priceOf(this.ctx, tr.tick);
    const { cfg } = this.ctx;
    if (tr.confirmed) {
      this.update(tr, now, `Pulled: ${fq(tr.cancelled)} cancelled vs ${fq(tr.executed)} executed.`);
      if (this.ctx.gateOpen) {
        this.ctx.emit({
          id: `pulled-${k}-${now}`,
          t: now,
          kind: 'liquidity_pulled',
          title: 'Major Liquidity Removed',
          side: tr.side,
          price: p,
          confidence: Math.round(40 + 60 * clamp01(tr.cancelled / Math.max(tr.peak, 1e-12))),
          explain: `A ${fq(tr.peak)} ${tr.side} order held ${(life / 1000).toFixed(1)}s at ${fp(this.ctx, p)} was cancelled (${fq(tr.cancelled)} removed without execution, ${fq(tr.executed)} executed).`,
          data: { peak: tr.peak, cancelled: tr.cancelled, executed: tr.executed, lifeMs: life },
        });
      }
    }
    // spoofing suspicion: large, short-lived, removed by cancellation as price approached
    const midTick = (this.ctx.book.bestBidTick + this.ctx.book.bestAskTick) / 2;
    const distNow = Math.abs(tr.tick - midTick);
    const cancelFrac = tr.cancelled / Math.max(tr.peak, 1e-12);
    const fl = (this.flicker.get(k) ?? []).filter((t) => now - t < 120_000);
    fl.push(now);
    this.flicker.set(k, fl);
    if (this.flicker.size > 2000) for (const [kk, v] of this.flicker) if (!v.length || now - v[v.length - 1] > 120_000) this.flicker.delete(kk);
    const approached = tr.distAtSeen > 0 ? 1 - distNow / tr.distAtSeen : 0;
    if (
      this.ctx.gateOpen &&
      life <= cfg.spoof.maxLifeMs &&
      cancelFrac >= cfg.spoof.minCancelFrac &&
      tr.executed <= 0.1 * tr.peak &&
      (approached >= 1 - cfg.spoof.approachFrac || fl.length >= 3)
    ) {
      const quick = 1 - life / cfg.spoof.maxLifeMs;
      const conf = Math.round(100 * clamp01(0.3 * clamp01(approached) + 0.25 * quick + 0.25 * clamp01((fl.length - 1) / 3) + 0.2 * clamp01(cancelFrac)));
      this.spoofed.set(k, now);
      if (conf >= cfg.minConfidence) {
        this.ctx.emit({
          id: `spoof-${k}-${now}`,
          t: now,
          kind: 'spoofing',
          title: 'Possible Spoofing (suspicion)',
          side: tr.side,
          price: p,
          confidence: conf,
          explain:
            `Suspicion only — intent cannot be proven from public data. ${fq(tr.peak)} ${tr.side} at ${fp(this.ctx, p)} lived ${(life / 1000).toFixed(1)}s, ` +
            `${(cancelFrac * 100).toFixed(0)}% cancelled, ${fq(tr.executed)} executed; distance to mid went ${tr.distAtSeen.toFixed(0)}→${distNow.toFixed(0)} ticks; ` +
            `${fl.length} large appear/cancel cycle(s) at this price in 2 min.`,
          data: { lifeMs: life, cancelFrac, flicker: fl.length },
        });
      }
    }
  }

  private update(tr: Track, now: number, note: string): void {
    if (!this.ctx.gateOpen) return;
    const p = priceOf(this.ctx, tr.tick);
    this.ctx.emit({
      id: `large-${this.key(tr.side, tr.tick)}-${tr.firstSeen}`,
      t: tr.firstSeen + this.ctx.cfg.large.minHoldMs,
      endT: now,
      kind: 'large_order',
      title: 'Large Limit Order',
      side: tr.side,
      price: p,
      confidence: tr.confidence,
      explain: `${note} Peak ${fq(tr.peak)}, held ${((now - tr.firstSeen) / 1000).toFixed(1)}s, replenished ${tr.replenishments}×, executed ${fq(tr.executed)}, cancelled ${fq(tr.cancelled)}.`,
      data: { peak: tr.peak, executed: tr.executed, cancelled: tr.cancelled, replenishments: tr.replenishments },
      status: tr.status,
    });
  }

  private confidence(tr: Track, now: number): number {
    const size = clamp01(0.5 + Math.log2(tr.peak / tr.threshold) / 2);
    const hold = clamp01((now - tr.firstSeen) / (4 * this.ctx.cfg.large.minHoldMs));
    const fl = this.flicker.get(this.key(tr.side, tr.tick))?.length ?? 0;
    const stable = 1 - clamp01(fl / 4);
    return Math.round(100 * (0.5 * size + 0.3 * hold + 0.2 * stable));
  }

  list(now: number): LargeOrder[] {
    const out: LargeOrder[] = [];
    for (const [k, tr] of this.tracks) {
      if (!tr.confirmed) continue;
      const live = tr.status === 'active' || tr.status === 'partially_filled';
      if (live) tr.confidence = this.confidence(tr, now);
      out.push({
        id: `large-${k}-${tr.firstSeen}`,
        side: tr.side,
        price: priceOf(this.ctx, tr.tick),
        size: tr.size,
        peak: tr.peak,
        firstSeen: tr.firstSeen,
        lastSeen: live ? now : tr.endT,
        holdMs: (live ? now : tr.endT) - tr.firstSeen,
        replenishments: tr.replenishments,
        executed: tr.executed,
        cancelled: tr.cancelled,
        status: tr.status,
        confidence: tr.confidence,
        threshold: tr.threshold,
        source: this.ctx.meta.source,
      });
    }
    return out.sort((a, b) => b.price - a.price);
  }
}
