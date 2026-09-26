// Trade/flow-driven detectors: absorption, sweep, stop-run, OBI imbalance, volume burst,
// delta divergence and spread expansion.
import type { Candle, Trade } from '../types.js';
import { RingSamples, clamp01 } from '../stats.js';
import { relativeVolume, isSpike } from '../volumeStats.js';
import { type DetectorContext, fp, fq, moveUnit } from './context.js';

interface WinTrade {
  t: number;
  p: number;
  q: number;
  s: 1 | -1;
}

export class FlowDetectors {
  private win: WinTrade[] = [];
  private winHead = 0;
  private absSamplesBuy: RingSamples;
  private absSamplesSell: RingSamples;
  private lastAbsSample = 0;
  private lastAbs: Record<'bid' | 'ask', { t: number; p: number }> = { bid: { t: 0, p: 0 }, ask: { t: 0, p: 0 } };
  private sec1: RingSamples;
  private curSec = 0;
  private curSecVol = 0;
  private curSecDelta = 0;
  private sweepVol: RingSamples;
  private lastSweep = 0;
  private activeSweep: { id: string; side: 1 | -1; lo: number; hi: number; vol: number; levels: Set<number>; lastT: number; t: number; conf: number } | null = null;
  private pendingRuns: { dir: 1 | -1; level: number; t: number; extreme: number; vol: number }[] = [];
  private obiSince = 0;
  private obiSide = 0;
  private obiFired = false;
  private spreads: RingSamples;
  private spreadSince = 0;
  private spreadFired = false;
  private bars: Candle[] = [];
  private barCvd: number[] = [];

  constructor(private ctx: DetectorContext) {
    this.absSamplesBuy = new RingSamples(3000);
    this.absSamplesSell = new RingSamples(3000);
    this.sec1 = new RingSamples(3600);
    this.sweepVol = new RingSamples(3600);
    this.spreads = new RingSamples(3000);
  }

  reset(): void {
    this.win = [];
    this.winHead = 0;
    this.pendingRuns = [];
    this.obiSince = 0;
    this.spreadSince = 0;
  }

  /** 1m bars (closed) for stop-run swing levels and divergence. */
  setBars(bars: Candle[], cvdAtClose: number[]): void {
    this.bars = bars;
    this.barCvd = cvdAtClose;
  }

  onTrade(tr: Trade): void {
    const { cfg } = this.ctx;
    this.win.push({ t: tr.t, p: tr.price, q: tr.qty, s: tr.side });
    const lim = tr.t - cfg.absorption.windowMs;
    while (this.winHead < this.win.length && this.win[this.winHead].t < lim) this.winHead++;
    if (this.winHead > 5000) {
      this.win = this.win.slice(this.winHead);
      this.winHead = 0;
    }
    // 1-second volume buckets -> burst detection & sweep baseline
    const sec = Math.floor(tr.t / 1000);
    if (sec !== this.curSec) {
      if (this.curSec) this.closeSecond(this.curSec * 1000 + 1000);
      this.curSec = sec;
      this.curSecVol = 0;
      this.curSecDelta = 0;
    }
    this.curSecVol += tr.qty;
    this.curSecDelta += tr.side * tr.qty;
    this.checkSweep(tr);
    this.checkStopRuns(tr);
  }

  private closeSecond(t: number): void {
    this.sec1.push(this.curSecVol);
    // accumulate into the burst bucket
    const bc = this.ctx.cfg.burst;
    const b = Math.floor((t - 1) / bc.bucketMs);
    if (b !== this.bucket) {
      if (this.bucket) this.closeBucket((this.bucket + 1) * bc.bucketMs);
      this.bucket = b;
      this.bucketVol = 0;
      this.bucketDelta = 0;
    }
    this.bucketVol += this.curSecVol;
    this.bucketDelta += this.curSecDelta;
  }

  private bucket = 0;
  private bucketVol = 0;
  private bucketDelta = 0;
  private buckets: number[] = [];
  private lastBurst = -Infinity;

  /** Volume burst: this bucket vs the rolling normal of the instrument (ratio to median and z-score). */
  private closeBucket(t: number): void {
    const bc = this.ctx.cfg.burst;
    const v = this.bucketVol;
    if (this.buckets.length >= bc.minSamples && this.ctx.gateOpen && t - this.lastBurst >= bc.cooldownMs) {
      const r = relativeVolume(v, this.buckets);
      if (isSpike(r, bc) && r.median > 0) {
        this.lastBurst = t;
        const buy = this.bucketDelta >= 0;
        const last = this.win[this.win.length - 1];
        const conf = Math.round(100 * clamp01(0.4 + 0.15 * Math.log2(Math.max(1, r.ratio)) + 0.1 * Math.max(0, r.z - bc.zMin) + 0.2 * clamp01(Math.abs(this.bucketDelta) / Math.max(v, 1e-12))));
        if (conf >= this.ctx.cfg.minConfidence)
          this.ctx.emit({
            id: `burst-${t}`,
            t,
            kind: 'volume_burst',
            title: 'Всплеск объёма',
            side: buy ? 'buy' : 'sell',
            price: last ? last.p : NaN,
            confidence: conf,
            explain: `${fq(v)} за ${bc.bucketMs / 1000} с: volumeRatio ${r.ratio.toFixed(2)} (к скользящей медиане ${fq(r.median)} за ${this.buckets.length} интервалов), volumeZScore ${r.z.toFixed(2)}; порог: ratio ≥ ${bc.ratioMin} ${bc.mode === 'and' ? 'и' : 'или'} z ≥ ${bc.zMin}; delta ${fq(this.bucketDelta)}.`,
            data: { volume: v, median: r.median, ratio: r.ratio, z: r.z, delta: this.bucketDelta },
          });
      }
    }
    this.buckets.push(v);
    if (this.buckets.length > bc.window) this.buckets.shift();
  }

  private checkSweep(tr: Trade): void {
    const sc = this.ctx.cfg.sweep;
    // an ongoing sweep keeps extending the same event while same-side aggression continues
    const a = this.activeSweep;
    if (a && tr.t - a.lastT <= sc.windowMs) {
      if (tr.side === a.side) {
        a.lastT = tr.t;
        a.vol += tr.qty;
        a.levels.add(tr.price);
        const extends_ = tr.side === 1 ? tr.price > a.hi : tr.price < a.lo;
        a.lo = Math.min(a.lo, tr.price);
        a.hi = Math.max(a.hi, tr.price);
        this.lastSweep = tr.t;
        if (extends_ && this.ctx.gateOpen) this.emitSweep(a);
      }
      return;
    }
    this.activeSweep = null;
    // collect same-direction trades within sweep window
    let lo = Infinity;
    let hi = -Infinity;
    let vol = 0;
    let n = 0;
    const levels = new Set<number>();
    for (let i = this.win.length - 1; i >= this.winHead; i--) {
      const w = this.win[i];
      if (tr.t - w.t > sc.windowMs) break;
      if (w.s !== tr.side) continue;
      lo = Math.min(lo, w.p);
      hi = Math.max(hi, w.p);
      vol += w.q;
      n++;
      levels.add(w.p);
    }
    const range = hi - lo;
    this.sweepVol.push(vol);
    const minRange = moveUnit(this.ctx, sc.minRangeAtr, sc.minRangeTicks);
    if (n < 3 || range < minRange || tr.t - this.lastSweep < sc.cooldownMs || this.sweepVol.size < 200) return;
    const pv = this.sweepVol.percentile(sc.volPercentile);
    // the sweep must end at the extreme in its direction (still pushing)
    const atExtreme = tr.side === 1 ? tr.price >= hi : tr.price <= lo;
    if (vol < pv || !atExtreme) return;
    this.lastSweep = tr.t;
    const conf = Math.round(100 * clamp01(0.4 + 0.3 * clamp01(range / (3 * minRange)) + 0.3 * clamp01(vol / (2 * pv))));
    this.activeSweep = { id: `sweep-${tr.t}-${tr.side}`, side: tr.side, lo, hi, vol, levels, lastT: tr.t, t: tr.t, conf };
    if (this.ctx.gateOpen && conf >= this.ctx.cfg.minConfidence) this.emitSweep(this.activeSweep, pv, minRange);
    // stop-run candidate if the sweep took out a recent swing extreme
    const lb = this.ctx.cfg.stopRun.lookbackBars;
    const bars = this.bars.slice(-lb);
    if (bars.length >= Math.min(10, lb)) {
      if (tr.side === 1) {
        const swing = Math.max(...bars.map((b) => b.h));
        if (hi > swing && lo <= swing + minRange) this.pendingRuns.push({ dir: 1, level: swing, t: tr.t, extreme: hi, vol });
      } else {
        const swing = Math.min(...bars.map((b) => b.l));
        if (lo < swing && hi >= swing - minRange) this.pendingRuns.push({ dir: -1, level: swing, t: tr.t, extreme: lo, vol });
      }
    }
  }

  private emitSweep(a: NonNullable<FlowDetectors['activeSweep']>, pv?: number, minRange?: number): void {
    const sc = this.ctx.cfg.sweep;
    const range = a.hi - a.lo;
    this.ctx.emit({
      id: a.id,
      t: a.t,
      endT: a.lastT,
      kind: 'sweep',
      title: a.side === 1 ? 'Sweep ликвидности (покупки)' : 'Sweep ликвидности (продажи)',
      side: a.side === 1 ? 'buy' : 'sell',
      price: a.side === 1 ? a.hi : a.lo,
      priceHi: a.hi,
      confidence: a.conf,
      explain:
        `Агрессивные ${a.side === 1 ? 'покупки прошли' : 'продажи прошли'} ${a.levels.size} ценовых уровней (${fp(this.ctx, a.lo)}–${fp(this.ctx, a.hi)}, диапазон ${fp(this.ctx, range)}` +
        (minRange !== undefined ? ` ≥ ${fp(this.ctx, minRange)}` : '') +
        `) за ${((a.lastT - a.t) / 1000 + sc.windowMs / 1000).toFixed(1)} с, объём ${fq(a.vol)}` +
        (pv !== undefined ? ` ≥ P${(sc.volPercentile * 100).toFixed(0)} ${fq(pv)}` : '') +
        '.',
      data: { lo: a.lo, hi: a.hi, volume: a.vol, levels: a.levels.size },
    });
    // extend a pending stop-run candidate created by this sweep
    for (const r of this.pendingRuns) if (r.t === a.t) r.extreme = a.side === 1 ? Math.max(r.extreme, a.hi) : Math.min(r.extreme, a.lo);
  }

  private checkStopRuns(tr: Trade): void {
    if (!this.pendingRuns.length) return;
    const keep = [];
    for (const r of this.pendingRuns) {
      if (tr.t - r.t > this.ctx.cfg.stopRun.reclaimMs) continue;
      if (r.dir === 1) r.extreme = Math.max(r.extreme, tr.price);
      else r.extreme = Math.min(r.extreme, tr.price);
      const reclaimed = r.dir === 1 ? tr.price < r.level : tr.price > r.level;
      if (!reclaimed) {
        keep.push(r);
        continue;
      }
      const over = Math.abs(r.extreme - r.level);
      const speed = 1 - (tr.t - r.t) / this.ctx.cfg.stopRun.reclaimMs;
      const unit = moveUnit(this.ctx, 0.2, 3);
      const conf = Math.round(100 * clamp01(0.45 + 0.3 * speed + 0.25 * clamp01(over / (2 * unit))));
      if (this.ctx.gateOpen && conf >= this.ctx.cfg.minConfidence)
        this.ctx.emit({
          id: `stoprun-${r.t}-${r.dir}`,
          t: tr.t,
          kind: 'stop_run',
          title: r.dir === 1 ? 'Паттерн stop-run над максимумом' : 'Паттерн stop-run под минимумом',
          side: r.dir === 1 ? 'sell' : 'buy',
          price: r.level,
          priceHi: r.extreme,
          confidence: conf,
          explain: `Sweep ${r.dir === 1 ? 'выше' : 'ниже'} наблюдаемого экстремума ${this.ctx.cfg.stopRun.lookbackBars} 1m-баров ${fp(this.ctx, r.level)} до ${fp(this.ctx, r.extreme)}, затем цена вернулась за уровень через ${((tr.t - r.t) / 1000).toFixed(1)} с. Это паттерн, а не доказательство исполнения конкретных стоп-заявок.`,
          data: { level: r.level, extreme: r.extreme, volume: r.vol },
        });
    }
    this.pendingRuns = keep;
  }

  /** Window-based absorption check (call ~2-4Hz). */
  evaluate(now: number): void {
    this.checkImbalance(now);
    this.checkSpread(now);
    const ac = this.ctx.cfg.absorption;
    let buyV = 0;
    let sellV = 0;
    let hi = -Infinity;
    let lo = Infinity;
    let hiT = 0;
    let loT = 0;
    let first: WinTrade | undefined;
    let last: WinTrade | undefined;
    for (let i = this.winHead; i < this.win.length; i++) {
      const w = this.win[i];
      if (now - w.t > ac.windowMs) continue;
      if (!first) first = w;
      last = w;
      if (w.s === 1) buyV += w.q;
      else sellV += w.q;
      if (w.p > hi) {
        hi = w.p;
        hiT = w.t;
      }
      if (w.p < lo) {
        lo = w.p;
        loT = w.t;
      }
    }
    if (now - this.lastAbsSample >= 1000) {
      this.lastAbsSample = now;
      this.absSamplesBuy.push(buyV);
      this.absSamplesSell.push(sellV);
    }
    if (!first || !last || this.absSamplesSell.size < ac.minSamples) return;
    const maxMove = moveUnit(this.ctx, ac.maxMoveAtr, ac.minMoveTicks);
    const holdMs = Math.min(3000, ac.windowMs / 3);
    // volume that kept hitting the extreme zone after the extreme was set
    let sellAtLow = 0;
    let buyAtHigh = 0;
    for (let i = this.winHead; i < this.win.length; i++) {
      const w = this.win[i];
      if (now - w.t > ac.windowMs) continue;
      if (w.s === -1 && w.t >= loT && w.p <= lo + maxMove) sellAtLow += w.q;
      if (w.s === 1 && w.t >= hiT && w.p >= hi - maxMove) buyAtHigh += w.q;
    }
    const pSell = this.absSamplesSell.percentile(ac.volPercentile);
    const pBuy = this.absSamplesBuy.percentile(ac.volPercentile);
    const tot = buyV + sellV;
    // sellers absorbed at the bid: heavy selling, no new low for holdMs while selling continued at the low,
    // total adverse move within maxMove
    if (sellV >= pSell && pSell > 0 && first.p - lo <= maxMove && sellV > buyV && now - loT >= holdMs && sellAtLow >= 0.5 * sellV) {
      this.absorb('bid', now, lo, sellV, pSell, first.p - lo, maxMove, (sellV - buyV) / tot);
    }
    if (buyV >= pBuy && pBuy > 0 && hi - first.p <= maxMove && buyV > sellV && now - hiT >= holdMs && buyAtHigh >= 0.5 * buyV) {
      this.absorb('ask', now, hi, buyV, pBuy, hi - first.p, maxMove, (buyV - sellV) / tot);
    }
  }

  private absorb(side: 'bid' | 'ask', now: number, price: number, vol: number, pv: number, move: number, maxMove: number, oneSided: number): void {
    const la = this.lastAbs[side];
    const ac = this.ctx.cfg.absorption;
    if (now - la.t < ac.cooldownMs && Math.abs(la.p - price) <= maxMove) return;
    const conf = Math.round(100 * clamp01(0.35 * clamp01(Math.log2(vol / pv) + 0.5) + 0.35 * (1 - move / maxMove) + 0.3 * clamp01(oneSided)));
    if (!this.ctx.gateOpen || conf < this.ctx.cfg.minConfidence) return;
    this.lastAbs[side] = { t: now, p: price };
    const zoneHi = side === 'bid' ? price + maxMove : price;
    const zoneLo = side === 'bid' ? price : price - maxMove;
    this.ctx.emit({
      id: `abs-${side}-${now}`,
      t: now,
      kind: 'absorption',
      title: side === 'bid' ? 'Поглощение: bid принимает продажи' : 'Поглощение: ask принимает покупки',
      side,
      price: zoneLo,
      priceHi: zoneHi,
      confidence: conf,
      explain: `${fq(vol)} агрессивных ${side === 'bid' ? 'продаж' : 'покупок'} за ${ac.windowMs / 1000} с (≥ P${(ac.volPercentile * 100).toFixed(0)} ${fq(pv)}) сдвинули цену лишь на ${fp(this.ctx, move)} (порог ${fp(this.ctx, maxMove)}); поток на ${(oneSided * 100).toFixed(0)}% односторонний — пассивная ликвидность ${side} поглотила его (absorbed).`,
      data: { volume: vol, move, oneSided },
    });
  }

  private checkImbalance(now: number): void {
    const s = this.ctx.stats;
    const ic = this.ctx.cfg.imbalance;
    if (!s) return;
    const side = s.obi >= ic.threshold ? 1 : s.obi <= -ic.threshold ? -1 : 0;
    if (side !== this.obiSide) {
      if (Math.abs(s.obi) < ic.threshold / 2 || side !== 0) {
        this.obiSide = side;
        this.obiSince = now;
        this.obiFired = false;
      }
      return;
    }
    if (side === 0 || this.obiFired || now - this.obiSince < ic.minMs) return;
    this.obiFired = true;
    const conf = Math.round(100 * clamp01(0.4 + 0.4 * clamp01((Math.abs(s.obi) - ic.threshold) / (1 - ic.threshold)) + 0.2 * clamp01((now - this.obiSince) / (3 * ic.minMs))));
    if (this.ctx.gateOpen && conf >= this.ctx.cfg.minConfidence)
      this.ctx.emit({
        id: `obi-${now}`,
        t: now,
        kind: 'imbalance',
        title: side > 0 ? 'Дисбаланс стакана (перевес bid)' : 'Дисбаланс стакана (перевес ask)',
        side: side > 0 ? 'bid' : 'ask',
        price: s.mid,
        confidence: conf,
        explain: `Дисбаланс топ-${ic.levels} уровней ${(s.obi * 100).toFixed(0)}% держится ≥ ${(ic.minMs / 1000).toFixed(0)} с; OFI(10 с) ${fq(s.ofi)}; microprice ${fp(this.ctx, s.microprice)} против mid ${fp(this.ctx, s.mid)}.`,
        data: { obi: s.obi, ofi: s.ofi },
      });
  }

  private checkSpread(now: number): void {
    const s = this.ctx.stats;
    if (!s || !isFinite(s.spread)) return;
    this.spreads.push(s.spread);
    if (this.spreads.size < 300) return;
    const med = this.spreads.percentile(0.5);
    const sc = this.ctx.cfg.spread;
    const wide = s.spread >= Math.max(sc.mult * med, med + sc.minTicks * this.ctx.tick);
    if (!wide) {
      this.spreadSince = 0;
      this.spreadFired = false;
      return;
    }
    if (!this.spreadSince) this.spreadSince = now;
    if (this.spreadFired || now - this.spreadSince < sc.minMs) return;
    this.spreadFired = true;
    if (this.ctx.gateOpen)
      this.ctx.emit({
        id: `spread-${now}`,
        t: now,
        kind: 'spread_expansion',
        title: 'Расширение спреда',
        price: s.mid,
        confidence: Math.round(100 * clamp01(0.5 + 0.1 * (s.spread / med))),
        explain: `Спред ${fp(this.ctx, s.spread)} = ${(s.spread / med).toFixed(1)}× медианы ${fp(this.ctx, med)} в течение ${((now - this.spreadSince) / 1000).toFixed(1)} с — ликвидность у лучшей цены снята.`,
        data: { spread: s.spread, median: med },
      });
  }

  /** Called when a 1m bar closes. Checks price/CVD divergence against the previous N bars. */
  onBarClose(bar: Candle, cvdAtClose: number): void {
    const n = this.ctx.cfg.divergence.lookbackBars;
    const prev = this.bars.slice(-n);
    const prevCvd = this.barCvd.slice(-n);
    if (prev.length < n) return;
    const maxH = Math.max(...prev.map((b) => b.h));
    const minL = Math.min(...prev.map((b) => b.l));
    const maxC = Math.max(...prevCvd);
    const minC = Math.min(...prevCvd);
    const cvdRange = Math.max(1e-12, maxC - minC);
    const bearish = bar.h > maxH && cvdAtClose < maxC;
    const bullish = bar.l < minL && cvdAtClose > minC;
    if (!(bearish || bullish) || !this.ctx.gateOpen) return;
    const gap = bearish ? (maxC - cvdAtClose) / cvdRange : (cvdAtClose - minC) / cvdRange;
    const conf = Math.round(100 * clamp01(0.4 + 0.6 * clamp01(gap)));
    if (conf < this.ctx.cfg.minConfidence) return;
    this.ctx.emit({
      id: `div-${bar.t}`,
      t: bar.t + 60_000,
      kind: 'delta_divergence',
      title: bearish ? 'Дивергенция дельты (медвежья)' : 'Дивергенция дельты (бычья)',
      side: bearish ? 'sell' : 'buy',
      price: bearish ? bar.h : bar.l,
      confidence: conf,
      explain: bearish
        ? `Новый максимум за ${n} баров ${fp(this.ctx, bar.h)}, но CVD ${fq(cvdAtClose)} ниже своего максимума ${fq(maxC)} — агрессивные покупки не подтвердили максимум.`
        : `Новый минимум за ${n} баров ${fp(this.ctx, bar.l)}, но CVD ${fq(cvdAtClose)} выше своего минимума ${fq(minC)} — агрессивные продажи не подтвердили минимум.`,
      data: { cvd: cvdAtClose },
    });
  }

  medianSecondVolume(): number {
    return this.sec1.size ? this.sec1.percentile(0.5) : NaN;
  }
}

