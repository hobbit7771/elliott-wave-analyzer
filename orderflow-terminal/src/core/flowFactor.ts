// Order-flow factor "flow7" (paper FORWARD TEST, not part of the managed portfolio): every week, long the perpetuals
// where aggressive buyers dominated over the last 7 days and short those where aggressive sellers did, sized inversely
// to volatility, equal gross long and short (market-neutral).
// flow7 = mean over the last 7 closed daily bars of (taker buy − taker sell) / volume = (2·bv − v) / v.
// Why: coins bought with market orders for a week outperform the rest the next week (README, round 18): rank IC of
// the 7-day return +0.063 (t 3.9) in 09.2021–02.2024 and +0.049 (t 3.2) in the untouched 03.2024–08.2026.
// Backtest (monthly top-50 Binance perps, weekly, 10 + 10 names, fees 0.075 %/side, gross 0.5 per side, no funding):
// +19.9 %/yr, vol 20 %, max drawdown 26 % over 5 years — but weak in the first half (Sharpe 0.45), so it runs on
// paper until the forward record agrees. PAPER ONLY — nothing is sent to an exchange.
import type { Candle } from './types.js';
import type { FactorParams } from './fundingFactor.js';

export const SITE_FLOW_PARAMS: FactorParams & { names: number; universe: number } = {
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
};

export const FLOW_BACKTEST = { annRet: 0.199, annVol: 0.203, maxDD: 0.26 };

export interface FlowInput {
  symbol: string;
  price: number;
  flow7: number;
  vol: number; // annualized volatility of daily returns (30 d)
  qv30: number; // 30-day quote volume
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
  return new Map([...side(rows.slice(-k), 1), ...side(rows.slice(0, k), -1)]);
}
