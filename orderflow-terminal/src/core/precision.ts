// Price / quantity helpers. Prices are kept as JS numbers but always snapped to the
// instrument tick grid through integer tick indices to avoid float drift in map keys.

export function decimalsOf(step: number): number {
  if (!isFinite(step) || step <= 0) return 8;
  const s = step.toString();
  if (s.includes('e-')) return parseInt(s.split('e-')[1], 10);
  const i = s.indexOf('.');
  return i < 0 ? 0 : s.length - i - 1;
}

export function toTick(price: number, tick: number): number {
  return Math.round(price / tick);
}

export function fromTick(idx: number, tick: number, decimals = decimalsOf(tick)): number {
  return +(idx * tick).toFixed(decimals);
}

export function snap(price: number, tick: number, decimals = decimalsOf(tick)): number {
  return fromTick(toTick(price, tick), tick, decimals);
}

/** floor price to a grouping bucket */
export function bucketFloor(price: number, step: number, decimals = decimalsOf(step)): number {
  return +(Math.floor(price / step + 1e-9) * step).toFixed(decimals);
}

export function fmt(v: number, decimals: number): string {
  return v.toFixed(decimals);
}

/** Compact quantity formatting (1.2k, 3.4M). */
export function fmtQty(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (a >= 1e4) return (v / 1e3).toFixed(1) + 'k';
  if (a >= 100) return v.toFixed(0);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(4);
}
