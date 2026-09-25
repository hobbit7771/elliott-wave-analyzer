// Depth-aware paper fills: a market order walks the visible book level by level.
// If the visible depth cannot absorb the size, the order is rejected (never filled at the best price).
export interface WalkResult {
  ok: boolean;
  avgPrice: number;
  filled: number;
  levelsUsed: number;
  worstPrice: number;
  reason?: string;
}

/** levels best-first as [price, qty]. side +1 = buy (consumes asks), -1 = sell (consumes bids). */
export function walkBook(levels: [number, number][], qty: number, maxLevels = Infinity): WalkResult {
  let left = qty;
  let notional = 0;
  let used = 0;
  let worst = NaN;
  for (const [p, q] of levels) {
    if (left <= 1e-15 || used >= maxLevels) break;
    const take = Math.min(q, left);
    notional += take * p;
    left -= take;
    used++;
    worst = p;
  }
  const filled = qty - Math.max(0, left);
  if (left > 1e-12 * Math.max(1, qty)) {
    return { ok: false, avgPrice: filled > 0 ? notional / filled : NaN, filled, levelsUsed: used, worstPrice: worst, reason: `visible depth (${used} levels) holds only ${filled} of ${qty}` };
  }
  return { ok: true, avgPrice: notional / qty, filled: qty, levelsUsed: used, worstPrice: worst };
}
