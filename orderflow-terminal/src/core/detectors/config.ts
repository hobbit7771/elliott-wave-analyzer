// Detector parameters. Defaults are dimensionless / adaptive (percentiles, fractions of depth, ATR multiples)
// so the same defaults work across instruments; every value can be overridden per instrument.

export interface DetectorConfig {
  /** fraction of mid-price around the touch analysed for resting liquidity */
  rangePct: number;
  staleMs: number;
  /** emit nothing below this confidence */
  minConfidence: number;
  large: {
    depthPct: number;         // level >= depthPct * side depth within range
    percentile: number;       // level >= this percentile of sampled level sizes
    minQty: number;           // absolute floor in base units (0 = auto)
    minHoldMs: number;
    minDistanceTicks: number; // ignore levels closer than this to mid
    atrAdjust: boolean;
    minSamples: number;       // warm-up: number of sampled level sizes required
  };
  iceberg: {
    minRefills: number;
    minTradedToDisplayed: number; // traded volume at level / max displayed
    minObservationMs: number;
    maxIdleMs: number;            // forget a level after this long without activity
    maxCancelRatio: number;       // spoof / noise exclusion
  };
  absorption: {
    windowMs: number;
    volPercentile: number;
    maxMoveAtr: number;     // allowed adverse move as fraction of 1m ATR
    minMoveTicks: number;
    minSamples: number;
    cooldownMs: number;
  };
  spoof: {
    maxLifeMs: number;
    minCancelFrac: number;
    approachFrac: number;
    maxDistPct: number;     // only levels this close to mid (fraction of price) can be spoof suspects
    cooldownMs: number;     // at most one suspicion per side per cooldown
  };
  pulled: {
    maxDistPct: number;     // only report pulls near the market
    cooldownMs: number;     // per side
  };
  cluster: {
    elevationMult: number;  // bucket considered elevated if >= mult * median bucket size
    minBuckets: number;
    maxGapBuckets: number;
    minHoldMs: number;
    topPerSide: number;     // only the N largest zones per side are announced as events
  };
  sweep: {
    windowMs: number;
    minRangeTicks: number;
    minRangeAtr: number;
    volPercentile: number;
    cooldownMs: number;
  };
  stopRun: {
    lookbackBars: number;
    reclaimMs: number;
  };
  imbalance: {
    threshold: number;  // |OBI|
    minMs: number;
    levels: number;
  };
  vacuum: {
    factor: number;      // bucket < factor * median => thin
    minBuckets: number;
    maxDistanceBuckets: number;
    cooldownMs: number;
  };
  /** volume burst: bucket volume vs the instrument's rolling normal (never absolute volume) */
  burst: {
    bucketMs: number;       // volume is summed per bucket
    window: number;         // rolling window of buckets for median / mean / std
    ratioMin: number;       // volumeRatio = bucket / rolling median
    zMin: number;           // volumeZScore = (bucket − mean) / std
    mode: 'or' | 'and';     // or: either condition, and: both
    minSamples: number;
    cooldownMs: number;
  };
  divergence: {
    lookbackBars: number;
  };
  spread: {
    mult: number;
    minTicks: number;
    minMs: number;
  };
}

export const DEFAULT_DETECTOR_CONFIG: DetectorConfig = {
  rangePct: 0.01,
  staleMs: 5000,
  minConfidence: 30,
  large: { depthPct: 0.02, percentile: 0.97, minQty: 0, minHoldMs: 10_000, minDistanceTicks: 0, atrAdjust: true, minSamples: 400 },
  iceberg: { minRefills: 3, minTradedToDisplayed: 1.5, minObservationMs: 4000, maxIdleMs: 90_000, maxCancelRatio: 0.6 },
  absorption: { windowMs: 10_000, volPercentile: 0.95, maxMoveAtr: 0.15, minMoveTicks: 2, minSamples: 60, cooldownMs: 15_000 },
  spoof: { maxLifeMs: 20_000, minCancelFrac: 0.8, approachFrac: 0.6, maxDistPct: 0.002, cooldownMs: 60_000 },
  pulled: { maxDistPct: 0.003, cooldownMs: 60_000 },
  cluster: { elevationMult: 2.5, minBuckets: 3, maxGapBuckets: 1, minHoldMs: 30_000, topPerSide: 3 },
  sweep: { windowMs: 1000, minRangeTicks: 5, minRangeAtr: 0.2, volPercentile: 0.95, cooldownMs: 3000 },
  stopRun: { lookbackBars: 30, reclaimMs: 60_000 },
  imbalance: { threshold: 0.6, minMs: 3000, levels: 10 },
  vacuum: { factor: 0.15, minBuckets: 5, maxDistanceBuckets: 60, cooldownMs: 60_000 },
  burst: { bucketMs: 10_000, window: 360, ratioMin: 1.5, zMin: 2, mode: 'and', minSamples: 60, cooldownMs: 30_000 },
  divergence: { lookbackBars: 10 },
  spread: { mult: 4, minTicks: 3, minMs: 1000 },
};

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };

export function mergeConfig(base: DetectorConfig, patch: DeepPartial<DetectorConfig> | undefined): DetectorConfig {
  const out = structuredClone(base) as unknown as Record<string, unknown>;
  if (!patch) return out as unknown as DetectorConfig;
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) continue;
    const cur = out[k];
    if (cur && typeof cur === 'object' && v && typeof v === 'object') {
      for (const [k2, v2] of Object.entries(v)) {
        if (v2 !== undefined && typeof v2 === typeof (cur as Record<string, unknown>)[k2]) (cur as Record<string, unknown>)[k2] = v2;
      }
    } else if (typeof v === typeof cur) out[k] = v;
  }
  return out as unknown as DetectorConfig;
}
