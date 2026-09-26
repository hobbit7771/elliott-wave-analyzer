// Strong D1 levels as known at a point in time. Uses ONLY the closed daily candles passed in (the caller
// guarantees they closed before the moment of evaluation), so a historical evaluation never sees the future.
//
// Candidates and the base ranking are the same as staticLevels (swing pivots clustered within 0.35×ATR,
// rejections, attempts, volume near the level estimated from daily OHLCV). On top of that:
//  - reaction quality: how far (in ATR_D1) and how fast price moved away after each rejection;
//  - chop / saw detection: closes inside the zone, alternating closes above/below, small displacement,
//    too-frequent retests -> CHOPPED (score penalised);
//  - acceptance beyond the zone (close outside + follow-through + no quick reclaim) -> BROKEN,
//    and FLIPPED when price later rejects the level from the other side;
//  - LevelStrengthScore 0–100 with a breakdown.
import type { Candle } from '../types.js';
import { atr } from '../indicators.js';

export type LevelStatus = 'STRONG' | 'CHOPPED' | 'BROKEN' | 'FLIPPED';

export interface DailyLevel {
  id: string;
  price: number;
  /** zone half-width (price units) */
  zone: number;
  /** role relative to the latest close: support below price, resistance above */
  role: 'support' | 'resistance';
  status: LevelStatus;
  strength: number;
  rejections: number;
  cleanReactions: number;
  sweepReclaims: number;
  attempts: number;
  volumeRel: number;
  avgReactionAtr: number;
  avgReactionDays: number;
  chopScore: number;
  lastTouch: number;
  brokenAt?: number;
  breakdown: Record<string, number>;
  why: string;
}

export interface DailyLevelParams {
  max: number;
  tolAtr: number;
  minRejections: number;
  spacingAtr: number;
  reactionDays: number;
  chopLookbackDays: number;
  chopThreshold: number;
  acceptDays: number;
  followThroughAtr: number;
  reclaimDays: number;
}

export const DEFAULT_DAILY_LEVEL_PARAMS: DailyLevelParams = {
  max: 10,
  tolAtr: 0.35,
  minRejections: 2,
  spacingAtr: 1.5,
  reactionDays: 5,
  chopLookbackDays: 90,
  chopThreshold: 0.55,
  acceptDays: 2,
  followThroughAtr: 0.5,
  reclaimDays: 3,
};

const DAY = 86_400_000;

export function analyzeDailyLevels(daily: readonly Candle[], p: DailyLevelParams = DEFAULT_DAILY_LEVEL_PARAMS, tick?: number): DailyLevel[] {
  const c = daily.filter((x) => x.h >= x.l && x.v >= 0);
  if (c.length < 30) return [];
  const a = atr(c as Candle[], 14);
  const atrNow = a[a.length - 1];
  if (!isFinite(atrNow) || atrNow <= 0) return [];
  const tol = p.tolAtr * atrNow;
  const lo = Math.min(...c.map((x) => x.l));
  const hi = Math.max(...c.map((x) => x.h));
  const lastClose = c[c.length - 1].c;
  const tN = c[c.length - 1].t;
  const t0 = c[0].t;

  // volume at price (daily volume spread uniformly over the day's range) — an estimate
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
  const volAt = (px: number) => {
    const k = Math.floor((px - lo) / bin);
    let s = 0;
    for (let b = Math.max(0, k - (bandBins >> 1)); b <= Math.min(nb - 1, k + (bandBins >> 1)); b++) s += vap[b];
    return s / avgBand;
  };

  // swing pivots clustered
  const piv: number[] = [];
  for (let i = 2; i < c.length - 2; i++) {
    if (c[i].h >= Math.max(c[i - 1].h, c[i - 2].h, c[i + 1].h, c[i + 2].h)) piv.push(c[i].h);
    if (c[i].l <= Math.min(c[i - 1].l, c[i - 2].l, c[i + 1].l, c[i + 2].l)) piv.push(c[i].l);
  }
  piv.sort((x, y) => x - y);
  const clusters: number[][] = [];
  for (const px of piv) {
    const last = clusters[clusters.length - 1];
    if (last && px - last[last.length - 1] <= tol) last.push(px);
    else clusters.push([px]);
  }

  const out: (DailyLevel & { baseScore: number })[] = [];
  for (const cl of clusters) {
    const price = cl[Math.floor(cl.length / 2)];
    let rejS = 0;
    let rejR = 0;
    let attempts = 0;
    let sweepReclaims = 0;
    let lastTouch = 0;
    const reactions: { atr: number; days: number }[] = [];
    const touchIdx: number[] = [];
    for (let i = 1; i < c.length; i++) {
      const d = c[i];
      const prevC = c[i - 1].c;
      if (!(d.l <= price + tol && d.h >= price - tol)) continue;
      lastTouch = d.t;
      touchIdx.push(i);
      const atrI = isFinite(a[i]) && a[i] > 0 ? a[i] : atrNow;
      let dir = 0;
      if (prevC > price + tol && d.c > price) {
        rejS++;
        dir = 1;
        if (d.l < price - tol) sweepReclaims++;
      } else if (prevC < price - tol && d.c < price) {
        rejR++;
        dir = -1;
        if (d.h > price + tol) sweepReclaims++;
      } else if (Math.abs(d.c - price) <= tol) attempts++;
      if (dir) {
        // reaction: best move away within the next N CLOSED days that exist in the input (all before T)
        let best = 0;
        let days = p.reactionDays + 1;
        for (let k = i + 1; k <= Math.min(c.length - 1, i + p.reactionDays); k++) {
          const mv = dir > 0 ? c[k].h - price : price - c[k].l;
          if (mv > best) best = mv;
          if (days > p.reactionDays && mv >= atrI) days = k - i;
        }
        reactions.push({ atr: best / atrI, days });
      }
    }
    const rejections = rejS + rejR;
    if (rejections < p.minRejections) continue;
    const volumeRel = volAt(price);
    const recency = tN > t0 ? (lastTouch - t0) / (tN - t0) : 0;
    const baseScore = rejections * 2 + attempts + (10 * Math.min(3, volumeRel)) / 3 + 2 * recency; // same as staticLevels
    const avgReactionAtr = reactions.length ? reactions.reduce((s, r) => s + Math.min(r.atr, 5), 0) / reactions.length : 0;
    const avgReactionDays = reactions.length ? reactions.reduce((s, r) => s + r.days, 0) / reactions.length : p.reactionDays + 1;
    const cleanReactions = reactions.filter((r) => r.atr >= 1).length;

    // chop / saw over the recent lookback
    const recent = touchIdx.filter((i) => c[i].t >= tN - p.chopLookbackDays * DAY);
    let inside = 0;
    let alternations = 0;
    let dispSum = 0;
    let prevSide = 0;
    for (const i of recent) {
      if (Math.abs(c[i].c - price) <= tol) inside++;
      const side = Math.sign(c[i].c - price);
      if (prevSide && side && side !== prevSide) alternations++;
      if (side) prevSide = side;
      let disp = 0;
      for (let k = i + 1; k <= Math.min(c.length - 1, i + 3); k++) disp = Math.max(disp, Math.abs(c[k].c - price));
      dispSum += disp / atrNow;
    }
    const nT = recent.length;
    const insideFrac = nT ? inside / nT : 0;
    const altRate = nT > 1 ? alternations / (nT - 1) : 0;
    const avgDisp = nT ? dispSum / nT : 1;
    const touchFreq = nT / p.chopLookbackDays;
    const chopScore = nT >= 6 ? 0.35 * insideFrac + 0.25 * altRate + 0.2 * (1 - Math.min(1, avgDisp)) + 0.2 * Math.min(1, touchFreq / 0.25) : 0;

    // acceptance beyond the zone (true break) — scan for the most recent one
    let brokenAt: number | undefined;
    let breakDir = 0;
    for (let i = p.acceptDays; i < c.length; i++) {
      const above = c.slice(i - p.acceptDays + 1, i + 1).every((d) => d.c > price + tol);
      const below = c.slice(i - p.acceptDays + 1, i + 1).every((d) => d.c < price - tol);
      if (!above && !below) continue;
      // a break crosses the level: the last close OUTSIDE the zone before the accepted run was on the other side
      // (a bounce away from a support is not a break of it)
      let k = i - p.acceptDays;
      while (k >= 0 && Math.abs(c[k].c - price) <= tol) k--;
      const wasOther = k >= 0 && (above ? c[k].c < price - tol : c[k].c > price + tol);
      if (!wasOther) continue;
      const dir = above ? 1 : -1;
      let follow = 0;
      let reclaimed = false;
      for (let k = i + 1; k <= Math.min(c.length - 1, i + p.reclaimDays); k++) {
        follow = Math.max(follow, dir > 0 ? c[k].h - price : price - c[k].l);
        if (dir > 0 ? c[k].c < price - tol : c[k].c > price + tol) reclaimed = true;
      }
      // follow-through measured on the accepted days themselves too
      for (let k = i - p.acceptDays + 1; k <= i; k++) follow = Math.max(follow, dir > 0 ? c[k].h - price : price - c[k].l);
      const confirmedWindow = i + p.reclaimDays <= c.length - 1;
      if (follow >= p.followThroughAtr * atrNow && !reclaimed && confirmedWindow) {
        brokenAt = c[i].t;
        breakDir = dir;
      }
    }
    let status: LevelStatus = 'STRONG';
    if (brokenAt !== undefined) {
      // FLIPPED: after the break, price came back and was rejected from the new side
      const after = c.filter((d) => d.t > brokenAt!);
      const flipped = after.some((d, j) => j > 0 && (breakDir > 0 ? d.l <= price + tol && d.c > price + tol : d.h >= price - tol && d.c < price - tol));
      status = flipped ? 'FLIPPED' : 'BROKEN';
    } else if (chopScore >= p.chopThreshold) status = 'CHOPPED';

    const br = {
      rejections: Math.min(1, rejections / 8) * 25,
      volume: Math.min(1, volumeRel / 3) * 20,
      reaction: Math.min(1, avgReactionAtr / 2.5) * 25,
      speed: Math.min(1, 1 / Math.max(1, avgReactionDays)) * 10,
      recency: recency * 10,
      sweepReclaims: Math.min(1, sweepReclaims / 3) * 10,
      chopPenalty: -chopScore * 30,
      brokenPenalty: status === 'BROKEN' ? -25 : 0,
    };
    let strength = Math.round(Math.max(0, Math.min(100, Object.values(br).reduce((s, x) => s + x, 0))));
    if (status === 'CHOPPED') strength = Math.min(strength, 40);
    const role = price <= lastClose ? 'support' : 'resistance';
    out.push({
      id: `L${Math.round(price / (tick || atrNow / 100))}`,
      price: tick ? Math.round(price / tick) * tick : price,
      zone: 0.5 * tol,
      role,
      status,
      strength,
      rejections,
      cleanReactions,
      sweepReclaims,
      attempts,
      volumeRel,
      avgReactionAtr,
      avgReactionDays,
      chopScore,
      lastTouch,
      brokenAt,
      breakdown: Object.fromEntries(Object.entries(br).map(([k, v]) => [k, Math.round(v)])),
      why: `отскоков ${rejections} (чистых реакций ≥1 ATR: ${cleanReactions}), ложных пробоев с возвратом ${sweepReclaims}, дней у уровня ${attempts}; средняя реакция ${avgReactionAtr.toFixed(1)} ATR за ${avgReactionDays.toFixed(1)} дн.; объём около уровня ≈${volumeRel.toFixed(1)}× среднего (оценка по дневным OHLCV); пила ${(chopScore * 100).toFixed(0)}%`,
      baseScore,
    });
  }
  // same ranking and spacing as staticLevels, up to p.max
  out.sort((x, y) => y.baseScore - x.baseScore);
  const picked: DailyLevel[] = [];
  for (const l of out) {
    if (picked.some((o) => Math.abs(o.price - l.price) < p.spacingAtr * atrNow)) continue;
    const { baseScore: _b, ...lv } = l;
    picked.push(lv);
    if (picked.length >= p.max) break;
  }
  return picked.sort((x, y) => y.price - x.price);
}
