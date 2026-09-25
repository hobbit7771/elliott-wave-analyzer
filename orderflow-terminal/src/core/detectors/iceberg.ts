// Probable iceberg / estimated hidden liquidity detector.
//
// Public depth never reveals hidden size. We estimate *probable* hidden liquidity at a price level from:
//   - executed volume at the level vs. its displayed size (volume traded beyond what was visible),
//   - repeated replenishment after executions (refills),
//   - the level holding (no trade-through) while aggressive volume hits it,
//   - concentration of same-direction aggression on that level, delta / CVD pressure against it,
//   - OFI and microprice leaning against the level, trade speed, time at the touch,
// and exclude large-but-ordinary orders (no refills), spoof suspects and cancel-heavy levels.
import type { BookSide, Trade } from '../types.js';
import type { FlowRecord } from '../orderbook.js';
import { RollingSum, clamp01 } from '../stats.js';
import { toTick } from '../precision.js';
import { type DetectorContext, fp, fq, priceOf } from './context.js';

interface LevelState {
  side: BookSide;
  tick: number;
  first: number;
  lastActivity: number;
  maxDisplayed: number;
  initialDisplayed: number;
  visibleExec: number;
  hidden: number;
  cancelled: number;
  added: number;
  refills: number;
  trades: number;
  broken: boolean;
  atTouchMs: number;
  lastTouchCheck: number;
  emittedConf: number;
  emittedId: string;
  lastEmit: number;
  cvdAtFirst: number;
  ofiAgainst: number;
  microAgainst: number;
  pendingDepletion: number;
}

export type IcebergType = 'probable_bid_iceberg' | 'probable_ask_iceberg' | 'absorption_iceberg' | 'replenishment_iceberg' | 'weak_suspicion';

export interface IcebergScore {
  confidence: number;
  type: IcebergType;
  features: Record<string, number>;
  eligible: boolean;
  reasons: string[];
}

/**
 * Estimated hidden liquidity consumed so far: volume executed at the level beyond the size that was
 * displayed when the level first got hit (covers both "executed without visible depletion" and
 * "visibly refilled after depletion").
 */
function estimatedHidden(s: LevelState): number {
  return Math.max(0, s.visibleExec + s.hidden - s.initialDisplayed);
}

export class IcebergDetector {
  private levels = new Map<string, LevelState>();
  /** aggressive volume by direction in the analysis window */
  private aggBuy = new RollingSum(30_000);
  private aggSell = new RollingSum(30_000);

  constructor(private ctx: DetectorContext, private isSpoofSuspect: (key: string) => boolean) {}

  reset(): void {
    this.levels.clear();
  }

  private key(side: BookSide, tick: number): string {
    return side[0] + tick;
  }

  onTrade(tr: Trade): void {
    (tr.side === 1 ? this.aggBuy : this.aggSell).add(tr.t, tr.qty);
    const t = toTick(tr.price, this.ctx.tick);
    // trade-through breaks levels on the opposite side of the aggressor
    for (const s of this.levels.values()) {
      if (s.broken) continue;
      if ((s.side === 'bid' && t < s.tick) || (s.side === 'ask' && t > s.tick)) {
        s.broken = true;
        s.lastActivity = tr.t;
        if (s.emittedId) this.emit(s, tr.t, 'broken');
      }
    }
  }

  onFlow(recs: FlowRecord[], now: number): void {
    for (const r of recs) {
      const traded = r.executed + r.hidden;
      const k = this.key(r.side, r.tick);
      let s = this.levels.get(k);
      if (!s) {
        if (traded <= 0) continue; // only track levels that are being hit
        s = {
          side: r.side,
          tick: r.tick,
          first: now,
          lastActivity: now,
          maxDisplayed: isNaN(r.prevQty) ? this.ctx.book.qtyAt(r.side, r.tick) : r.prevQty,
          initialDisplayed: isNaN(r.prevQty) ? this.ctx.book.qtyAt(r.side, r.tick) : r.prevQty,
          visibleExec: 0,
          hidden: 0,
          cancelled: 0,
          added: 0,
          refills: 0,
          trades: 0,
          broken: false,
          atTouchMs: 0,
          lastTouchCheck: now,
          emittedConf: 0,
          emittedId: '',
          lastEmit: 0,
          cvdAtFirst: this.ctx.cvd,
          ofiAgainst: 0,
          microAgainst: 0,
          pendingDepletion: 0,
        };
        this.levels.set(k, s);
      }
      if (s.broken) continue;
      const disp = isNaN(r.qty) ? this.ctx.book.qtyAt(r.side, r.tick) : r.qty;
      if (!isNaN(r.prevQty) && r.prevQty > s.maxDisplayed) s.maxDisplayed = r.prevQty;
      if (disp > s.maxDisplayed) s.maxDisplayed = disp;
      s.visibleExec += r.executed;
      s.hidden += r.hidden;
      s.cancelled += r.cancelled; // may be negative for late-trade corrections
      if (traded > 0) {
        s.trades++;
        s.lastActivity = now;
        s.pendingDepletion += traded;
      }
      const refillMin = Math.max(0.05 * s.maxDisplayed, isFinite(this.ctx.medianTradeQty) ? this.ctx.medianTradeQty : 0);
      // refill evidence #1: executions not reflected in the displayed size
      if (r.hidden > 0 && r.hidden >= refillMin) {
        s.refills++;
        s.pendingDepletion = 0;
      }
      // refill evidence #2: size added right after an execution depleted the level
      if (r.added > 0) {
        s.added += r.added;
        if (s.pendingDepletion >= refillMin && r.added >= 0.5 * Math.min(s.pendingDepletion, s.maxDisplayed)) s.refills++;
        s.pendingDepletion = 0;
      }
    }
  }

  /** Periodic evaluation (call on every book update or ~4Hz). */
  evaluate(now: number): void {
    const { book, cfg, stats } = this.ctx;
    this.aggBuy.evict(now);
    this.aggSell.evict(now);
    for (const [k, s] of this.levels) {
      if (now - s.lastActivity > cfg.iceberg.maxIdleMs || (s.broken && now - s.lastActivity > 30_000)) {
        this.levels.delete(k);
        continue;
      }
      if (s.broken) continue;
      const dt = now - s.lastTouchCheck;
      s.lastTouchCheck = now;
      const atTouch = s.side === 'bid' ? book.bestBidTick === s.tick : book.bestAskTick === s.tick;
      if (atTouch) s.atTouchMs += dt;
      if (stats) {
        // OFI < 0 = selling pressure at the touch (against a bid), > 0 buying (against an ask)
        if ((s.side === 'bid' && stats.ofi < 0) || (s.side === 'ask' && stats.ofi > 0)) s.ofiAgainst += dt;
        if ((s.side === 'bid' && stats.microprice < stats.mid) || (s.side === 'ask' && stats.microprice > stats.mid)) s.microAgainst += dt;
      }
      const sc = this.score(s, now);
      if (!sc.eligible || !this.ctx.gateOpen) continue;
      if (sc.confidence < cfg.minConfidence) continue;
      if (!s.emittedId) {
        s.emittedId = `ice-${k}-${s.first}`;
        s.emittedConf = sc.confidence;
        s.lastEmit = now;
        this.emit(s, now, 'active', sc);
      } else if (Math.abs(sc.confidence - s.emittedConf) >= 5 && now - s.lastEmit > 2000) {
        s.emittedConf = sc.confidence;
        s.lastEmit = now;
        this.emit(s, now, 'active', sc);
      }
    }
  }

  score(s: LevelState, now: number): IcebergScore {
    const ic = this.ctx.cfg.iceberg;
    const traded = s.visibleExec + s.hidden;
    const disp = Math.max(s.maxDisplayed, 1e-12);
    const estHidden = estimatedHidden(s);
    const obs = now - s.first;
    const reasons: string[] = [];
    const cancelBase = s.added + s.maxDisplayed;
    const cancelRatio = cancelBase > 0 ? Math.max(0, s.cancelled) / cancelBase : 0;
    const spoof = this.isSpoofSuspect(this.key(s.side, s.tick));
    // the level is "hit" by aggressors of the opposite direction
    const aggInWindow = s.side === 'bid' ? this.aggSell.sum : this.aggBuy.sum;
    const concentration = aggInWindow > 0 ? clamp01(traded / aggInWindow) : 0;
    const cvdAgainst = s.side === 'bid' ? s.cvdAtFirst - this.ctx.cvd : this.ctx.cvd - s.cvdAtFirst;

    const f = {
      tradedToDisplayed: traded / disp,
      refills: s.refills,
      holdSec: obs / 1000,
      atTouchFrac: obs > 0 ? s.atTouchMs / obs : 0,
      concentration,
      cvdAgainstRatio: traded > 0 ? clamp01(cvdAgainst / traded) : 0,
      ofiAgainstFrac: obs > 0 ? s.ofiAgainst / obs : 0,
      microAgainstFrac: obs > 0 ? s.microAgainst / obs : 0,
      tradesPerSec: obs > 0 ? s.trades / (obs / 1000) : 0,
      hidden: s.hidden,
      estHidden,
      cancelRatio,
    };
    let eligible = true;
    if (s.refills < ic.minRefills) (eligible = false), reasons.push(`refills ${s.refills} < ${ic.minRefills}`);
    if (f.tradedToDisplayed < ic.minTradedToDisplayed) (eligible = false), reasons.push(`traded/displayed ${f.tradedToDisplayed.toFixed(2)} < ${ic.minTradedToDisplayed}`);
    if (obs < ic.minObservationMs) (eligible = false), reasons.push('observation too short');
    if (s.broken) (eligible = false), reasons.push('level broken');
    if (cancelRatio > ic.maxCancelRatio) (eligible = false), reasons.push(`cancel ratio ${cancelRatio.toFixed(2)} (spoof/noise filter)`);
    if (spoof) (eligible = false), reasons.push('level flagged as spoof suspect');
    if (estHidden <= 0) (eligible = false), reasons.push('no volume beyond displayed size');

    const s1 = clamp01((f.tradedToDisplayed - 1) / 4);
    const s2 = clamp01((s.refills - ic.minRefills + 1) / (ic.minRefills * 2));
    const s3 = clamp01(obs / 30_000);
    const s4 = clamp01(f.atTouchFrac);
    const s5 = clamp01(0.5 * concentration + 0.5 * f.cvdAgainstRatio);
    const s6 = clamp01(f.tradesPerSec / 5);
    const s7 = clamp01(0.5 * f.ofiAgainstFrac + 0.5 * f.microAgainstFrac);
    let conf = 100 * (0.25 * s1 + 0.2 * s2 + 0.12 * s3 + 0.15 * s4 + 0.15 * s5 + 0.05 * s6 + 0.08 * s7);
    conf *= 1 - clamp01(cancelRatio);
    conf = Math.round(conf);
    let type: IcebergType;
    if (conf < 45) type = 'weak_suspicion';
    else if (s1 >= 0.6 && s4 >= 0.5 && s5 >= 0.5) type = 'absorption_iceberg';
    else if (s.refills >= 2 * ic.minRefills) type = 'replenishment_iceberg';
    else type = s.side === 'bid' ? 'probable_bid_iceberg' : 'probable_ask_iceberg';
    return { confidence: conf, type, features: { ...f, s1, s2, s3, s4, s5, s6, s7 }, eligible, reasons };
  }

  private emit(s: LevelState, now: number, status: string, sc?: IcebergScore): void {
    if (!this.ctx.gateOpen) return;
    const score = sc ?? this.score(s, now);
    const p = priceOf(this.ctx, s.tick);
    const f = score.features;
    const typeLabel: Record<IcebergType, string> = {
      probable_bid_iceberg: 'Probable Bid Iceberg',
      probable_ask_iceberg: 'Probable Ask Iceberg',
      absorption_iceberg: 'Absorption Iceberg (probable)',
      replenishment_iceberg: 'Replenishment Iceberg (probable)',
      weak_suspicion: 'Weak Iceberg Suspicion',
    };
    const traded = s.visibleExec + s.hidden;
    this.ctx.emit({
      id: s.emittedId || `ice-${this.key(s.side, s.tick)}-${s.first}`,
      t: s.emittedId ? now : now,
      kind: 'iceberg',
      title: typeLabel[score.type],
      subtype: score.type,
      side: s.side,
      price: p,
      confidence: status === 'broken' ? s.emittedConf : score.confidence,
      status,
      endT: status === 'broken' ? now : undefined,
      explain:
        (status === 'broken' ? 'Level was traded through — iceberg estimate closed. ' : '') +
        `Probable Iceberg / Estimated Hidden Liquidity ≈ ${fq(f.estHidden)} at ${fp(this.ctx, p)} (${s.side}). ` +
        `Traded ${fq(traded)} vs max displayed ${fq(s.maxDisplayed)} (${f.tradedToDisplayed.toFixed(1)}×; ${fq(s.hidden)} executed with no visible depletion), ${s.refills} refills, ` +
        `held ${(f.holdSec).toFixed(0)}s (${(f.atTouchFrac * 100).toFixed(0)}% at the touch), ${(f.concentration * 100).toFixed(0)}% of opposing aggression in 30s hit this level, ` +
        `CVD against level ${(f.cvdAgainstRatio * 100).toFixed(0)}%, OFI against ${(f.ofiAgainstFrac * 100).toFixed(0)}% of time, cancel ratio ${(f.cancelRatio * 100).toFixed(0)}%. ` +
        'Inferred from public trades + depth; hidden size is never directly visible.',
      data: {
        estimatedHidden: +f.estHidden.toFixed(6),
        traded: +traded.toFixed(6),
        maxDisplayed: s.maxDisplayed,
        refills: s.refills,
        icebergConfidence: score.confidence,
      },
    });
  }

  /** Active candidates (for DOM / debugging). */
  candidates(now: number): { side: BookSide; price: number; confidence: number; hidden: number; refills: number; eligible: boolean }[] {
    const out = [];
    for (const s of this.levels.values()) {
      if (s.broken) continue;
      const sc = this.score(s, now);
      out.push({ side: s.side, price: priceOf(this.ctx, s.tick), confidence: sc.confidence, hidden: estimatedHidden(s), refills: s.refills, eligible: sc.eligible });
    }
    return out;
  }
}
