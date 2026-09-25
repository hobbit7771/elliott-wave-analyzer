// Static support / resistance levels from daily candles: prices where a lot of volume traded and price
// repeatedly turned away (or needed several attempts to break through). At most `max` levels.
//
// Method (deterministic, no look-ahead within the input):
// 1. Volume-at-price ≈ each day's volume spread uniformly over its high–low range (Binance gives no daily
//    volume profile, so this is an approximation and is labelled as such).
// 2. Candidate prices = swing highs / lows (fractals of ±2 days), clustered within a tolerance of
//    0.35 × ATR(14, daily).
// 3. For each candidate: rejections (the day's wick reached the level but the close stayed on the
//    approach side), break attempts (days that closed within tolerance before a close beyond), and the
//    share of traded volume within ±tolerance.
// 4. Score = rejections × 2 + attempts + 10 × volume share (relative to the average band), + recency bonus;
//    levels need ≥ 2 rejections; the strongest are taken with a minimum spacing of 1.5 × ATR.
import type { Candle } from './types.js';
import { atr } from './indicators.js';

export interface StaticLevel {
  price: number;
  kind: 'support' | 'resistance' | 'both';
  rejections: number;
  attempts: number;
  /** share of all traded volume within ±tolerance of the level, relative to the average band (1 = average) */
  volumeRel: number;
  lastTouch: number;
  score: number;
  why: string;
}

export function staticLevels(daily: Candle[], opts: { max?: number; tick?: number; lastPrice?: number } = {}): StaticLevel[] {
  const max = opts.max ?? 4;
  const c = daily.filter((x) => x.h >= x.l && x.v >= 0);
  if (c.length < 30) return [];
  const a = atr(c, 14);
  const atrNow = a[a.length - 1];
  if (!isFinite(atrNow) || atrNow <= 0) return [];
  const tol = 0.35 * atrNow;
  const lo = Math.min(...c.map((x) => x.l));
  const hi = Math.max(...c.map((x) => x.h));

  // 1. volume at price (uniform per day)
  const bin = Math.max(tol / 2, (hi - lo) / 2000);
  const nb = Math.max(1, Math.ceil((hi - lo) / bin) + 1);
  const vap = new Float64Array(nb);
  for (const d of c) {
    const b0 = Math.floor((d.l - lo) / bin);
    const b1 = Math.floor((d.h - lo) / bin);
    const per = d.v / (b1 - b0 + 1);
    for (let b = b0; b <= b1; b++) vap[b] += per;
  }
  const totalV = vap.reduce((s, x) => s + x, 0) || 1;
  const bandBins = Math.max(1, Math.round((2 * tol) / bin));
  const avgBand = (totalV / nb) * bandBins;
  const volAt = (p: number) => {
    const k = Math.floor((p - lo) / bin);
    let s = 0;
    for (let b = Math.max(0, k - Math.floor(bandBins / 2)); b <= Math.min(nb - 1, k + Math.floor(bandBins / 2)); b++) s += vap[b];
    return s / avgBand;
  };

  // 2. swing pivots, clustered
  const piv: number[] = [];
  for (let i = 2; i < c.length - 2; i++) {
    if (c[i].h >= Math.max(c[i - 1].h, c[i - 2].h, c[i + 1].h, c[i + 2].h)) piv.push(c[i].h);
    if (c[i].l <= Math.min(c[i - 1].l, c[i - 2].l, c[i + 1].l, c[i + 2].l)) piv.push(c[i].l);
  }
  piv.sort((x, y) => x - y);
  const clusters: number[][] = [];
  for (const p of piv) {
    const last = clusters[clusters.length - 1];
    if (last && p - last[last.length - 1] <= tol) last.push(p);
    else clusters.push([p]);
  }

  // 3. evaluate each cluster centre
  const t0 = c[0].t;
  const tN = c[c.length - 1].t;
  const cands: StaticLevel[] = [];
  for (const cl of clusters) {
    const price = cl[Math.floor(cl.length / 2)];
    let rejS = 0; // support rejections (came from above, wick below/at, closed above)
    let rejR = 0;
    let attempts = 0;
    let lastTouch = 0;
    for (let i = 1; i < c.length; i++) {
      const d = c[i];
      const prevC = c[i - 1].c;
      const touches = d.l <= price + tol && d.h >= price - tol;
      if (!touches) continue;
      lastTouch = d.t;
      if (prevC > price + tol && d.c > price) rejS++;
      else if (prevC < price - tol && d.c < price) rejR++;
      else if (Math.abs(d.c - price) <= tol) attempts++;
    }
    const rejections = rejS + rejR;
    if (rejections < 2) continue;
    const volumeRel = volAt(price);
    const recency = tN > t0 ? (lastTouch - t0) / (tN - t0) : 0;
    const score = rejections * 2 + attempts + 10 * Math.min(3, volumeRel) / 3 + 2 * recency;
    const kind = rejS && rejR ? 'both' : rejS ? 'support' : 'resistance';
    cands.push({
      price: opts.tick ? Math.round(price / opts.tick) * opts.tick : price,
      kind,
      rejections,
      attempts,
      volumeRel,
      lastTouch,
      score,
      why: `${kind === 'support' ? 'поддержка' : kind === 'resistance' ? 'сопротивление' : 'поддержка/сопротивление'}: отскоков ${rejections}, дней у уровня до пробоя ${attempts}, объём около уровня ≈${volumeRel.toFixed(1)}× среднего (оценка по дневным OHLCV)`,
    });
  }

  // 4. strongest, spaced apart
  cands.sort((x, y) => y.score - x.score);
  const out: StaticLevel[] = [];
  for (const l of cands) {
    if (out.some((o) => Math.abs(o.price - l.price) < 1.5 * atrNow)) continue;
    out.push(l);
    if (out.length >= max) break;
  }
  return out.sort((x, y) => y.price - x.price);
}
