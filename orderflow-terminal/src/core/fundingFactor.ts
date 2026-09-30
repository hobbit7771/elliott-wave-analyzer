// Cross-sectional funding factor (paper): every week, long the perpetuals with the LOWEST trailing funding and short
// the ones with the HIGHEST, sized inversely to volatility, equal gross long and short (market-neutral).
// Why: high funding = crowded longs; those coins then do worse (measured in README, rounds 2 and 5: IC −0.05,
// t −2…−4), and the short leg also receives the funding the crowd pays. PAPER ONLY — nothing is sent to an exchange.
//
// Research (35 Binance perps 2021–2026, fees 0.075 %/side, weekly, terciles): +29 %/yr, vol 18.5 %, Sharpe 1.48,
// max drawdown 24 %; beta to BTC ≈ 0; Sharpe ≥ 1.03 when any single coin is removed, 0.98 without 2021, 1.17 with
// double fees. In a portfolio with the trend model and funding carry (correlations −0.11…+0.04): Sharpe 1.99.
import type { Funding } from './carry.js';

export interface FactorParams {
  lookbackDays: number;
  quantile: number; // share of the universe on each side
  rebalanceDays: number;
  volTarget: number; // per-coin annual vol used for the inverse-vol weights
  gross: number; // gross exposure per side as a fraction of capital
  feePerSide: number;
  capital: number;
  minCoins: number;
}

export const SITE_FACTOR_PARAMS: FactorParams = {
  lookbackDays: 7,
  quantile: 1 / 3,
  rebalanceDays: 7,
  volTarget: 0.2,
  gross: 0.5,
  feePerSide: 0.00075, // Bybit perp taker 0.055 % + slippage
  capital: 10_000,
  minCoins: 10,
};

export interface FactorInput {
  symbol: string;
  price: number;
  settlements: Funding[];
  vol: number; // annualized volatility of daily returns (30 d)
}

export interface FactorPos {
  symbol: string;
  qty: number; // signed contracts (coins): > 0 long, < 0 short
  funding: number; // funding received (+) / paid (−), USDT
  fees: number;
  lastSettle: number;
  px: number; // last known price
}

export interface FactorState {
  params: FactorParams;
  startedAt: number;
  lastRebalance: number;
  cash: number; // capital + realized trading P&L + funding − fees (positions valued separately)
  positions: FactorPos[];
  last: { t: number; long: string[]; short: string[] } | null;
  equity: { t: number; eq: number }[];
}

const DAY = 86_400_000;

export function newFactorState(now: number, params: FactorParams = SITE_FACTOR_PARAMS): FactorState {
  return { params, startedAt: now, lastRebalance: 0, cash: params.capital, positions: [], last: null, equity: [] };
}

export function meanFunding(f: readonly Funding[], now: number, days: number): number {
  let sum = 0;
  let n = 0;
  for (const x of f) if (x.t >= now - days * DAY && x.t < now) {
    sum += x.rate;
    n++;
  }
  return n >= days * 2 ? sum / days : NaN;
}

/** Target signed notional per coin (USDT), or null when there are too few coins with data. */
export function targets(inputs: readonly FactorInput[], now: number, p: FactorParams): Map<string, number> | null {
  const rows = inputs
    .map((x) => ({ ...x, f: meanFunding(x.settlements, now, p.lookbackDays) }))
    .filter((x) => isFinite(x.f) && x.price > 0 && x.vol > 0);
  if (rows.length < p.minCoins) return null;
  rows.sort((a, b) => a.f - b.f);
  const k = Math.max(1, Math.floor(rows.length * p.quantile));
  const longs = rows.slice(0, k);
  const shorts = rows.slice(-k);
  const side = (xs: typeof rows, sign: 1 | -1) => {
    const iv = xs.map((x) => Math.min(2, p.volTarget / x.vol));
    const sum = iv.reduce((a, b) => a + b, 0);
    return xs.map((x, i) => [x.symbol, (sign * p.gross * p.capital * iv[i]) / sum] as const);
  };
  return new Map([...side(longs, 1), ...side(shorts, -1)]);
}

/** Funding settlements after the last counted one: longs pay positive funding, shorts receive it. */
export function accrueFactor(st: FactorState, byPrice: ReadonlyMap<string, { price: number; settlements: Funding[] }>, now: number): void {
  for (const pos of st.positions) {
    const q = byPrice.get(pos.symbol);
    if (!q) continue;
    if (q.price > 0) pos.px = q.price;
    for (const s of q.settlements) if (s.t > pos.lastSettle && s.t <= now) {
      const amt = -pos.qty * q.price * s.rate;
      pos.funding += amt;
      st.cash += amt;
      pos.lastSettle = s.t;
    }
  }
}

/** Weekly rebalance to the targets (trades only the difference; fees on the traded notional). */
export function rebalance(st: FactorState, inputs: readonly FactorInput[], now: number): boolean {
  const p = st.params;
  const day = Math.floor(now / DAY) * DAY;
  if (now < day + 5 * 60_000 || (st.lastRebalance && day < st.lastRebalance + p.rebalanceDays * DAY)) return false;
  const tg = targets(inputs, now, p);
  if (!tg) return false;
  applyTargets(st, tg, new Map(inputs.map((x) => [x.symbol, x.price])), now);
  return true;
}

/** Trades the book to the target signed notionals (only the difference; fees on the traded notional). */
export function applyTargets(st: FactorState, tg: ReadonlyMap<string, number>, prices: ReadonlyMap<string, number>, now: number): void {
  const p = st.params;
  const day = Math.floor(now / DAY) * DAY;
  const cur = new Map(st.positions.map((x) => [x.symbol, x]));
  const next: FactorPos[] = [];
  for (const sym of new Set([...cur.keys(), ...tg.keys()])) {
    const price = prices.get(sym);
    const pos = cur.get(sym) ?? { symbol: sym, qty: 0, funding: 0, fees: 0, lastSettle: now, px: 0 };
    if (!price || !(price > 0)) {
      if (pos.qty !== 0) next.push(pos); // no price: keep until it comes back
      continue;
    }
    const want = (tg.get(sym) ?? 0) / price;
    const dq = want - pos.qty;
    if (dq !== 0) {
      const fee = Math.abs(dq) * price * p.feePerSide;
      st.cash -= dq * price + fee;
      pos.fees += fee;
      pos.qty = want;
    }
    pos.px = price;
    if (pos.qty !== 0) next.push(pos);
  }
  st.positions = next;
  st.lastRebalance = day;
  st.last = { t: now, long: [...tg].filter(([, v]) => v > 0).map(([s]) => s), short: [...tg].filter(([, v]) => v < 0).map(([s]) => s) };
}

export function factorEquity(st: FactorState, price: (s: string) => number | undefined): number {
  return st.cash + st.positions.reduce((a, x) => a + x.qty * (price(x.symbol) ?? x.px), 0);
}
