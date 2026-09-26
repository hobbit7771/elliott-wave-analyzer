// Relative-volume statistics shared by the live burst detector and the level/setup engine.
// Volume is always judged against the instrument's own recent normal, never in absolute units.

export function median(xs: readonly number[]): number {
  if (!xs.length) return NaN;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export interface RelVolume {
  /** current / rolling median */
  ratio: number;
  /** (current − mean) / std of the rolling window */
  z: number;
  median: number;
}

export function relativeVolume(current: number, window: readonly number[]): RelVolume {
  const med = median(window);
  let mean = 0;
  for (const x of window) mean += x;
  mean /= window.length || 1;
  let v = 0;
  for (const x of window) v += (x - mean) ** 2;
  // variance floor (20% of the mean): a nearly flat window must not turn a small uptick into a huge z-score
  const sd = Math.max(Math.sqrt(v / Math.max(1, window.length - 1)), 0.2 * mean);
  const z = sd > 0 ? (current - mean) / sd : 0;
  return { ratio: med > 0 ? current / med : NaN, z, median: med };
}

/** Least-squares slope of ys against 0..n-1, normalised by the mean of ys (fraction per bar). */
export function relSlope(ys: readonly number[]): number {
  const n = ys.length;
  if (n < 3) return 0;
  let sx = 0;
  let sy = 0;
  let sxy = 0;
  let sxx = 0;
  for (let i = 0; i < n; i++) {
    sx += i;
    sy += ys[i];
    sxy += i * ys[i];
    sxx += i * i;
  }
  const den = n * sxx - sx * sx;
  const slope = den ? (n * sxy - sx * sy) / den : 0;
  const mean = sy / n;
  return mean > 0 ? slope / mean : 0;
}

/** Threshold test with calibratable parameters: `or` = either condition, `and` = both. */
export function isSpike(r: RelVolume, p: { ratioMin: number; zMin: number; mode: 'or' | 'and' }): boolean {
  const a = r.ratio >= p.ratioMin;
  const b = r.z >= p.zMin;
  return p.mode === 'and' ? a && b : a || b;
}
