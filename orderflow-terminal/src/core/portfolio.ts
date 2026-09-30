// Portfolio risk manager (paper) over the three strategies the site runs:
//   trend  — the site engine's setups (trend breakout + Gerchik levels), risk `trendRisk` of capital per trade (R units);
//   carry  — long spot + short perpetual (market-neutral, not scaled: its volatility is tiny);
//   factor — cross-sectional funding factor, weight `factorWeight` of its own book.
// Rule (research, README round 7): scale the DIRECTIONAL part (trend + factor) by
//   k = min(1, long-run daily vol / last-30-day daily vol)
// i.e. cut exposure when the book is more volatile than usual (volatility clustering, Moreira & Muir). On 06.2021–09.2026:
// Sharpe 1.99 → 2.25 (both halves better), worst 30 days −6.4 % → −4.7 %, return 25.6 % → 23.4 %/yr.
// Risk parity between the sleeves raised the drawdown and a drawdown brake lowered the Sharpe ratio — not used.
// Limits: net exposure of one coin across the sleeves ≤ `maxCoinExposure` of equity (flagged, shown on the site).

export interface PortfolioParams {
  capital: number;
  trendRisk: number; // fraction of capital risked per trend trade (1R)
  factorWeight: number;
  volWindowDays: number;
  minHistoryDays: number; // below this the multiplier stays 1
  maxCoinExposure: number;
}

export const SITE_PORTFOLIO_PARAMS: PortfolioParams = {
  capital: 10_000,
  trendRisk: 0.0025,
  factorWeight: 0.5,
  volWindowDays: 30,
  minHistoryDays: 45,
  maxCoinExposure: 0.15,
};

/** Sleeve levels at one moment: cumulative trend R since the start, carry and factor equity (their own books). */
export interface Snap {
  t: number;
  trendR: number;
  carry: number;
  factor: number;
}

export interface DayRet {
  day: number;
  trend: number;
  carry: number;
  factor: number; // already multiplied by factorWeight
}

const DAY = 86_400_000;

/** Daily returns (as fractions of the portfolio) from the last snapshot of each UTC day. */
export function dailyReturns(snaps: readonly Snap[], p: PortfolioParams): DayRet[] {
  const lastOfDay = new Map<number, Snap>();
  for (const s of snaps) lastOfDay.set(Math.floor(s.t / DAY), s);
  const days = [...lastOfDay.keys()].sort((a, b) => a - b);
  const out: DayRet[] = [];
  for (let i = 1; i < days.length; i++) {
    const a = lastOfDay.get(days[i - 1])!;
    const b = lastOfDay.get(days[i])!;
    out.push({
      day: days[i] * DAY,
      trend: (b.trendR - a.trendR) * p.trendRisk,
      carry: a.carry > 0 ? b.carry / a.carry - 1 : 0,
      factor: a.factor > 0 ? (b.factor / a.factor - 1) * p.factorWeight : 0,
    });
  }
  return out;
}

const std = (x: readonly number[]): number => {
  if (x.length < 2) return NaN;
  const m = x.reduce((a, b) => a + b, 0) / x.length;
  return Math.sqrt(x.reduce((a, b) => a + (b - m) ** 2, 0) / (x.length - 1));
};

/** Risk multiplier for the directional sleeves, from the returns up to (not including) the day it applies to. */
export function riskMultiplier(rets: readonly DayRet[], p: PortfolioParams): number {
  if (rets.length < p.minHistoryDays) return 1;
  const d = rets.map((r) => r.trend + r.factor);
  const longRun = std(d);
  const recent = std(d.slice(-p.volWindowDays));
  if (!(longRun > 0) || !(recent > 0)) return 1;
  return Math.min(1, longRun / recent);
}

/** Managed equity path: each day's directional return scaled by the multiplier known the day before; carry as is. */
export function managedEquity(rets: readonly DayRet[], p: PortfolioParams): { day: number; eq: number; k: number }[] {
  let eq = p.capital;
  const out: { day: number; eq: number; k: number }[] = [];
  for (let i = 0; i < rets.length; i++) {
    const k = riskMultiplier(rets.slice(0, i), p);
    const r = rets[i];
    eq *= 1 + k * (r.trend + r.factor) + r.carry;
    out.push({ day: r.day, eq, k });
  }
  return out;
}

export function maxDrawdown(path: readonly { eq: number }[]): number {
  let peak = -Infinity;
  let dd = 0;
  for (const x of path) {
    peak = Math.max(peak, x.eq);
    dd = Math.max(dd, 1 - x.eq / peak);
  }
  return dd;
}

/** Net notional per coin (USDT, + long / − short) across the sleeves, and the coins above the limit. */
export function exposures(items: readonly { symbol: string; notional: number; sleeve: string }[], equity: number, p: PortfolioParams) {
  const by = new Map<string, { net: number; parts: Record<string, number> }>();
  for (const it of items) {
    const e = by.get(it.symbol) ?? { net: 0, parts: {} };
    e.net += it.notional;
    e.parts[it.sleeve] = (e.parts[it.sleeve] ?? 0) + it.notional;
    by.set(it.symbol, e);
  }
  return [...by]
    .map(([symbol, e]) => ({ symbol, net: e.net, share: equity > 0 ? e.net / equity : 0, parts: e.parts, overLimit: equity > 0 && Math.abs(e.net) / equity > p.maxCoinExposure }))
    .sort((a, b) => Math.abs(b.net) - Math.abs(a.net));
}

/** Backtest expectations per sleeve (research, 06.2021–09.2026; README rounds 5–7): annual return and volatility. */
export const BACKTEST: Record<'trend' | 'carry' | 'factor' | 'total', { annRet: number; annVol: number; note: string }> = {
  trend: { annRet: 0.109, annVol: 0.099, note: 'тренд, риск 0,25 %/сделку' },
  carry: { annRet: 0.046, annVol: 0.005, note: 'кэрри, ротация до 10 монет' },
  factor: { annRet: 0.145, annVol: 0.093, note: '½ фактора фандинга' },
  total: { annRet: 0.234, annVol: 0.095, note: 'портфель с риск-менеджером' },
};

/**
 * Monte-Carlo drawdown of the backtested portfolio (06.2021-09.2026; days reshuffled 3 000 times, also in 20/30-day
 * blocks - same result): the maximum drawdown one should expect from the same returns in another order. The historical
 * 9.4 % sits at the 76th percentile. Idea from the MQL5 code base ("Portfolio Correlation Analyzer"), README round 12.
 */
export const BACKTEST_DD = { p50: 0.08, p95: 0.12, p99: 0.15 };

export function drawdownVerdict(dd: number): string {
  if (dd > BACKTEST_DD.p99) return 'выше, чем в 99 % вариантов истории — стратегия ведёт себя иначе, проверить';
  if (dd > BACKTEST_DD.p95) return 'редкая просадка (хуже 95 % вариантов истории), но ещё допустимая';
  if (dd > BACKTEST_DD.p50) return 'обычная для этой стратегии просадка';
  return 'в пределах обычного';
}

/**
 * Live vs backtest: the return expected over `days` and its 1-sigma band (i.i.d. daily returns: sd grows with √time);
 * z = (live − expected) / sd. |z| < 1: as expected; z < −2: the live result is unlikely under the backtest — review.
 */
export function trackVsBacktest(liveRet: number, days: number, annRet: number, annVol: number): { expected: number; sd: number; z: number; verdict: string } {
  const expected = annRet * (days / 365);
  const sd = annVol * Math.sqrt(Math.max(days, 1) / 365);
  const z = sd > 0 ? (liveRet - expected) / sd : 0;
  const verdict = days < 30 ? 'мало данных' : z < -2 ? 'хуже бэктеста — проверить' : z < -1 ? 'ниже ожиданий, в пределах разброса' : z > 2 ? 'заметно лучше бэктеста' : 'в пределах ожидаемого';
  return { expected, sd, z, verdict };
}
