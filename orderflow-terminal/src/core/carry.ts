// Funding carry (cash-and-carry): long spot + short USDT perpetual of the same coin, so the price cancels out and the
// position collects the perpetual funding that longs pay shorts. Market-neutral; the risks are funding turning
// negative, the spot–perp basis moving, fees and exchange risk. PAPER ONLY: nothing is ever sent to an exchange.
//
// Research (README, scripts/experiments/carry): 35 Binance coins 2020–2026 with real funding, spot and perp daily
// closes and taker fees on both legs: rotating into the coins whose 7-day funding exceeds 10 % a year (exit below
// 3 %, up to 10 slots) returned ≈ 9–10 % a year on capital with a max drawdown ≈ 1 %, but the return depends on the
// regime (2021: +30 %, 2022 and 2025–26: ≈ 0 %), as Schmeling, Schrimpf & Todorov (BIS, 2023) also document.
// Added to the trend model on the same capital it raised the Sharpe ratio from 1.09 to 1.55 and cut the drawdown.
//
// Rules (once a day, on the first update after 00:05 UTC, from data known at that moment):
//  - trailing APR = mean funding per day over the last `lookbackDays` × 365;
//  - close a position whose trailing APR fell to ≤ exitApr (or whose quotes are missing);
//  - open the coins with the highest trailing APR > entryApr into the free slots;
//  - each slot's notional = capital / (1 + marginFrac) / slots (spot bought in full + perp margin);
//  - fees: feePerSide × notional on entry and on exit (both legs, taker);
//  - funding: every settlement after the entry adds rate × notional (negative rates are paid);
//  - basis P&L: notional × ((spot / spot0 − 1) − (perp / perp0 − 1)).

export interface Funding {
  t: number;
  rate: number;
}

export interface CarryParams {
  entryApr: number;
  exitApr: number;
  slots: number;
  lookbackDays: number;
  feePerSide: number;
  capital: number;
  marginFrac: number;
}

export const SITE_CARRY_PARAMS: CarryParams = {
  entryApr: 0.1,
  exitApr: 0.03,
  slots: 10,
  lookbackDays: 7,
  feePerSide: 0.0016, // Bybit spot taker 0.1 % + perp taker 0.055 %, rounded up
  capital: 10_000,
  marginFrac: 0.5,
};

export interface CarryQuote {
  symbol: string; // perpetual symbol, e.g. BTCUSDT (the spot pair has the same name)
  spot: number;
  perp: number;
  funding: number; // current (next settlement) rate
  nextFundingTime: number;
  turnover: number; // 24 h perp turnover, USDT
  settlements: Funding[]; // history, oldest first
}

export interface CarryPos {
  symbol: string;
  openedAt: number;
  notional: number;
  spot0: number;
  perp0: number;
  aprAtEntry: number;
  funding: number; // accumulated funding received (USDT)
  fees: number; // fees paid so far (USDT)
  lastSettle: number; // time of the last counted settlement
}

export interface CarryClosed extends CarryPos {
  closedAt: number;
  spot1: number;
  perp1: number;
  basis: number;
  net: number;
  reason: string;
}

export interface CarryState {
  params: CarryParams;
  startedAt: number;
  lastDecisionDay: number;
  positions: CarryPos[];
  closed: CarryClosed[];
  equity: { t: number; eq: number }[];
}

const DAY = 86_400_000;

export function newCarryState(now: number, params: CarryParams = SITE_CARRY_PARAMS): CarryState {
  return { params, startedAt: now, lastDecisionDay: 0, positions: [], closed: [], equity: [] };
}

/** Mean funding per day over [now − days, now) × 365; NaN with fewer than days × 2 settlements (8-hourly data). */
export function trailingApr(f: readonly Funding[], now: number, days: number): number {
  let sum = 0;
  let n = 0;
  for (const x of f) if (x.t >= now - days * DAY && x.t < now) {
    sum += x.rate;
    n++;
  }
  return n >= days * 2 ? (sum / days) * 365 : NaN;
}

export const slotNotional = (p: CarryParams): number => p.capital / (1 + p.marginFrac) / p.slots;
export const basisPnl = (pos: CarryPos, spot: number, perp: number): number => pos.notional * (spot / pos.spot0 - 1 - (perp / pos.perp0 - 1));

/** Add every funding settlement after the last counted one (up to `now`) to the held positions. */
export function accrue(st: CarryState, quotes: ReadonlyMap<string, CarryQuote>, now: number): void {
  for (const pos of st.positions) {
    const q = quotes.get(pos.symbol);
    if (!q) continue;
    for (const s of q.settlements) if (s.t > pos.lastSettle && s.t > pos.openedAt && s.t <= now) {
      pos.funding += s.rate * pos.notional; // the short perp receives positive funding
      pos.lastSettle = s.t;
    }
  }
}

export function close(st: CarryState, pos: CarryPos, q: CarryQuote | undefined, now: number, reason: string): void {
  const spot1 = q?.spot ?? pos.spot0;
  const perp1 = q?.perp ?? pos.perp0;
  const fees = pos.fees + pos.notional * st.params.feePerSide;
  const basis = basisPnl(pos, spot1, perp1);
  st.closed.push({ ...pos, fees, closedAt: now, spot1, perp1, basis, net: pos.funding + basis - fees, reason });
  if (st.closed.length > 500) st.closed.shift();
  st.positions = st.positions.filter((x) => x !== pos);
}

/** The daily decision: returns true if it ran (once per UTC day, after 00:05). */
export function decide(st: CarryState, quotes: ReadonlyMap<string, CarryQuote>, now: number): boolean {
  const day = Math.floor(now / DAY) * DAY;
  if (st.lastDecisionDay >= day || now < day + 5 * 60_000) return false;
  const p = st.params;
  accrue(st, quotes, now);
  const apr = new Map<string, number>();
  for (const [s, q] of quotes) apr.set(s, trailingApr(q.settlements, now, p.lookbackDays));
  for (const pos of [...st.positions]) {
    const a = apr.get(pos.symbol);
    const q = quotes.get(pos.symbol);
    if (!q || !(q.spot > 0 && q.perp > 0)) close(st, pos, q, now, 'нет котировок');
    else if (!(a !== undefined && a > p.exitApr)) close(st, pos, q, now, `фандинг упал до ${a !== undefined && isFinite(a) ? (a * 100).toFixed(1) : '—'} % годовых`);
  }
  const held = new Set(st.positions.map((x) => x.symbol));
  const cand = [...apr].filter(([s, a]) => isFinite(a) && a > p.entryApr && !held.has(s) && (quotes.get(s)?.spot ?? 0) > 0 && (quotes.get(s)?.perp ?? 0) > 0).sort((a, b) => b[1] - a[1]);
  const notional = slotNotional(p);
  for (const [s, a] of cand) {
    if (st.positions.length >= p.slots) break;
    const q = quotes.get(s)!;
    const lastSettle = q.settlements.length ? q.settlements[q.settlements.length - 1].t : now;
    st.positions.push({ symbol: s, openedAt: now, notional, spot0: q.spot, perp0: q.perp, aprAtEntry: a, funding: 0, fees: notional * p.feePerSide, lastSettle: Math.max(lastSettle, now) });
  }
  st.lastDecisionDay = day;
  return true;
}

export interface CarryTotals {
  realized: number;
  funding: number;
  basis: number;
  fees: number;
  unrealized: number;
  equity: number;
  returnPct: number;
}

export function totals(st: CarryState, quotes: ReadonlyMap<string, CarryQuote>): CarryTotals {
  const realized = st.closed.reduce((a, x) => a + x.net, 0);
  let funding = st.closed.reduce((a, x) => a + x.funding, 0);
  let basis = st.closed.reduce((a, x) => a + x.basis, 0);
  let fees = st.closed.reduce((a, x) => a + x.fees, 0);
  let unrealized = 0;
  for (const pos of st.positions) {
    const q = quotes.get(pos.symbol);
    const b = q ? basisPnl(pos, q.spot, q.perp) : 0;
    unrealized += pos.funding + b - pos.fees;
    funding += pos.funding;
    basis += b;
    fees += pos.fees;
  }
  const equity = st.params.capital + realized + unrealized;
  return { realized, funding, basis, fees, unrealized, equity, returnPct: ((equity - st.params.capital) / st.params.capital) * 100 };
}
