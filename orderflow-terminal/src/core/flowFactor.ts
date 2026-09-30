// Order-flow factor "flow7" (paper FORWARD TEST, not part of the managed portfolio): every week, long the perpetuals
// where aggressive buyers dominated over the last 7 days and short those where aggressive sellers did, sized inversely
// to volatility, market-neutral (beta-neutral, see below).
// flow7 = mean over the last 7 closed daily bars of (taker buy − taker sell) / volume = (2·bv − v) / v.
// Why: coins bought with market orders for a week outperform the rest the next week (README, round 18): rank IC of
// the 7-day return +0.063 (t 3.9) in 09.2021–02.2024 and +0.049 (t 3.2) in the untouched 03.2024–08.2026.
// The short side is scaled so the book's beta to the equal-weight market is zero (round 20: without it the book was
// quietly short the market, beta −0.13…−0.17, and part of its backtest profit was the falling market).
// Backtest (monthly top-50 Binance perps 09.2021–08.2026, weekly, 10 + 10 names, fees 0.075 %/side, long gross 0.5,
// beta-neutral, no funding): +12.9 %/yr, vol 17.7 %, max drawdown 24 % — weak in the first half (+8.8 %/yr at full
// gross), so it runs on paper until the forward record agrees. PAPER ONLY — nothing is sent to an exchange.
import type { Candle } from './types.js';
import type { FactorParams } from './fundingFactor.js';

export const SITE_FLOW_PARAMS: FactorParams & { names: number; universe: number; betaNeutral: boolean } = {
  lookbackDays: 7,
  quantile: 0, // unused: a fixed number of names per side
  names: 10,
  universe: 50, // most liquid USDT perpetuals by 30-day quote volume
  rebalanceDays: 7,
  volTarget: 0.6, // typical altcoin vol: caps a very calm coin at 2× the average weight
  gross: 0.5,
  feePerSide: 0.00075,
  capital: 10_000,
  minCoins: 30,
  betaNeutral: true,
};

export const FLOW_BACKTEST = { annRet: 0.129, annVol: 0.177, maxDD: 0.24 };

export interface FlowInput {
  symbol: string;
  price: number;
  flow7: number;
  vol: number; // annualized volatility of daily returns (30 d)
  qv30: number; // 30-day quote volume
  beta?: number; // 60-day beta to the equal-weight market of the universe
}

/** Features from closed daily bars (oldest first); NaN when the history is too short or has no volume. */
export function flowFeatures(daily: readonly Candle[]): { flow7: number; vol: number; qv30: number } {
  const n = daily.length;
  if (n < 31) return { flow7: NaN, vol: NaN, qv30: NaN };
  let f = 0;
  for (const k of daily.slice(-7)) {
    if (!(k.v > 0)) return { flow7: NaN, vol: NaN, qv30: NaN };
    f += (2 * k.bv - k.v) / k.v;
  }
  const last = daily.slice(-31);
  const r = last.slice(1).map((k, i) => Math.log(k.c / last[i].c));
  const m = r.reduce((a, b) => a + b, 0) / r.length;
  const vol = Math.sqrt(r.reduce((a, b) => a + (b - m) ** 2, 0) / (r.length - 1)) * Math.sqrt(365);
  const qv30 = last.slice(1).reduce((a, k) => a + k.v * k.c, 0);
  return { flow7: f / 7, vol, qv30 };
}

/** Target signed notional per coin (USDT), or null when there are too few coins with data. */
export function flowTargets(inputs: readonly FlowInput[], p: typeof SITE_FLOW_PARAMS): Map<string, number> | null {
  const rows = inputs
    .filter((x) => isFinite(x.flow7) && isFinite(x.qv30) && x.price > 0 && x.vol > 0)
    .sort((a, b) => b.qv30 - a.qv30)
    .slice(0, p.universe);
  if (rows.length < p.minCoins) return null;
  rows.sort((a, b) => a.flow7 - b.flow7);
  const k = Math.min(p.names, Math.floor(rows.length / 2));
  const side = (xs: typeof rows, sign: 1 | -1) => {
    const iv = xs.map((x) => Math.min(2, p.volTarget / x.vol));
    const sum = iv.reduce((a, b) => a + b, 0);
    return xs.map((x, i) => [x.symbol, (sign * p.gross * p.capital * iv[i]) / sum] as const);
  };
  const longs = side(rows.slice(-k), 1);
  let shorts = side(rows.slice(0, k), -1);
  // beta-neutral: short gross such that Σ w·β of the shorts equals that of the longs (capped at 3× the long gross)
  const b = (sym: string) => rows.find((x) => x.symbol === sym)?.beta;
  const bl = longs.reduce((a, [s2, w]) => a + w * (b(s2) ?? 1), 0);
  const bs = shorts.reduce((a, [s2, w]) => a - w * (b(s2) ?? 1), 0);
  if (p.betaNeutral && bl > 0 && bs > 0) shorts = shorts.map(([s2, w]) => [s2, w * Math.min(3, bl / bs)] as const);
  return new Map([...longs, ...shorts]);
}

/** 60-day beta of each coin to the equal-weight market of the given coins (closed daily bars, oldest first). */
export function marketBetas(daily: ReadonlyMap<string, readonly Candle[]>, days = 60): Map<string, number> {
  const rets = new Map<string, Map<number, number>>();
  for (const [sym, k] of daily) {
    const m = new Map<number, number>();
    for (let i = Math.max(1, k.length - days); i < k.length; i++) if (k[i].c > 0 && k[i - 1].c > 0) m.set(k[i].t, Math.log(k[i].c / k[i - 1].c));
    rets.set(sym, m);
  }
  const sum = new Map<number, { s: number; n: number }>();
  for (const m of rets.values()) for (const [t, r] of m) {
    const x = sum.get(t) ?? { s: 0, n: 0 };
    x.s += r;
    x.n++;
    sum.set(t, x);
  }
  const mkt = new Map([...sum].filter(([, x]) => x.n >= 5).map(([t, x]) => [t, x.s / x.n]));
  const out = new Map<string, number>();
  for (const [sym, m] of rets) {
    const pts = [...m].filter(([t]) => mkt.has(t)).map(([t, r]) => [r, mkt.get(t)!] as const);
    if (pts.length < 40) continue;
    const mr = pts.reduce((a, [r]) => a + r, 0) / pts.length;
    const mm = pts.reduce((a, [, x]) => a + x, 0) / pts.length;
    const cov = pts.reduce((a, [r, x]) => a + (r - mr) * (x - mm), 0);
    const v = pts.reduce((a, [, x]) => a + (x - mm) ** 2, 0);
    if (v > 0) out.set(sym, cov / v);
  }
  return out;
}
