// Short-term setups from "Street Smarts" (L. B. Raschke & L. A. Connors, 1995), on the same closed 15m bar
// stream as the other engines. A "day" is a UTC day (crypto trades 24/7); daily candles are closed UTC days.
//
//  • TURTLE SOUP        today trades below the prior 20-day low (made ≥ 4 sessions earlier) → buy stop a little
//                       above that prior low, today only; stop under today's low. Mirror for shorts.
//  • TURTLE SOUP +1     yesterday made a new 20-day low (prior low ≥ 3 sessions earlier) and CLOSED at/below the
//                       prior low → today a buy stop at the prior low; stop under the lower of the two days' lows.
//  • 80-20              yesterday opened in the top 20 % of its range and closed in the bottom 20 % → today, after
//                       price trades below yesterday's low, a buy stop at yesterday's low; stop under today's low.
//  • MOMENTUM PINBALL   LBR/RSI (3-period RSI of the 1-day change) < 30 at yesterday's close → buy stop at the
//                       high of today's first hour; stop at its low. > 70: mirror.
//  • ID/NR4             yesterday was an inside day with the narrowest range of the last 4 → today a buy stop above
//                       its high and a sell stop below its low (one cancels the other); stop at the other side.
//  • HOLY GRAIL         ADX(14) > 30 with the trend's DI on top, the first pullback to the 20 EMA → buy stop above
//                       the high of the bar that touched it; stop at the swing low; target the prior swing high.
//
// Exits (as in the book: short holding periods with a trailing stop): the initial stop, an optional target in R,
// trailing to the previous day's low (high) once a day has closed in profit, and a time exit at the close of the
// holdDays-th day. Orders are created at the CLOSE of a 15m bar and fill only on later bars; if a bar reaches
// both the stop and the target the stop is assumed first; Bybit fees and stop slippage are charged in R.
import type { Candle } from '../types.js';
import type { DailyLevel } from './dailyLevels.js';
import type { Feat, LevelState, Setup, SetupOutcome } from './setupEngine.js';

export type RaschkeSetup = 'TURTLE_SOUP' | 'TURTLE_SOUP_PLUS_ONE' | 'EIGHTY_TWENTY' | 'MOMENTUM_PINBALL' | 'ID_NR4' | 'HOLY_GRAIL';

export interface RaschkeParams {
  model: 'raschke';
  ltfMs: number;
  setups: RaschkeSetup[];
  lookback: number; // Turtle Soup channel (20 days)
  tsMinAge: number; // the prior extreme must be at least this many sessions old (4)
  tsPlusOneMinAge: number; // (3)
  offsetAtr: number; // "5–10 ticks": entry offset in ATR(D1)
  eightyTwentyTestAtr: number; // how far below yesterday's low price must trade first (ATR(D1))
  pinballLow: number; // LBR/RSI thresholds
  pinballHigh: number;
  hgTf: 'D1' | 'H4';
  hgAdx: number; // ADX threshold (30)
  hgValidBars: number; // bars (of hgTf) the Holy Grail buy stop stays valid
  minRiskAtr: number; // floor for the initial stop distance (0 = the book's stop as is)
  targetR: number; // fixed target in R (Infinity: none; the Holy Grail uses the prior swing extreme)
  holdDays: number; // time exit at the close of this day after entry (1 = the entry day)
  trailPrevDay: boolean; // after a day closes in profit, trail the stop to that day's low (high)
  exitIfNotProfitableAtClose: boolean; // Pinball / ID-NR4 rule: flat at the close unless in profit
  trendFilter: 'none' | 'sma50'; // optional context filter (not in the book): trade only with the D1 trend
  feeMaker: number;
  feeTaker: number;
  slippage: number;
}

/** The book's rules as written (with a fixed trailing / time exit), no tuning. */
export const BOOK_RASCHKE_PARAMS: RaschkeParams = {
  model: 'raschke',
  ltfMs: 15 * 60_000,
  setups: ['TURTLE_SOUP', 'TURTLE_SOUP_PLUS_ONE', 'EIGHTY_TWENTY', 'MOMENTUM_PINBALL', 'ID_NR4', 'HOLY_GRAIL'],
  lookback: 20,
  tsMinAge: 4,
  tsPlusOneMinAge: 3,
  offsetAtr: 0.02,
  eightyTwentyTestAtr: 0.03,
  pinballLow: 30,
  pinballHigh: 70,
  hgTf: 'D1',
  hgAdx: 30,
  hgValidBars: 3,
  minRiskAtr: 0,
  targetR: Infinity,
  holdDays: 3,
  trailPrevDay: true,
  exitIfNotProfitableAtClose: true,
  trendFilter: 'none',
  feeMaker: 0.0002,
  feeTaker: 0.00055,
  slippage: 0.0002,
};

interface Pending {
  setup: RaschkeSetup;
  dir: 1 | -1;
  price: number; // stop order
  sl: number | ((dayLow: number, dayHigh: number) => number); // fixed, or from the day's extreme at fill time
  target: number | null; // price target (Holy Grail)
  level: number;
  expires: number; // time (ms) after which the order is cancelled
  oco?: string; // one-cancels-other group
  why: string;
  createdAt: number;
}

type Open = Setup & { _sl: number; _entryFee: number; _entryDay: number; _setup: RaschkeSetup; _target: number | null };

const DAY = 86_400_000;
const H4 = 4 * 3_600_000;
const NO_FEAT: Feat = { dist: NaN, volDecay: NaN, volSlope: 0, contraction: NaN, impulse: NaN, candleRangeAtr: NaN, approachVelocity: 0, pullbackDepth: NaN, barsToLevel: NaN };
const tr = (b: Candle, pc: number) => Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc));
const NAMES: Record<RaschkeSetup, string> = {
  TURTLE_SOUP: 'Turtle Soup',
  TURTLE_SOUP_PLUS_ONE: 'Turtle Soup +1',
  EIGHTY_TWENTY: '80-20',
  MOMENTUM_PINBALL: 'Momentum Pinball',
  ID_NR4: 'ID/NR4',
  HOLY_GRAIL: 'Holy Grail',
};

// ---------------- indicators (closed candles only) ----------------

export function ema(values: number[], n: number): number[] {
  const k = 2 / (n + 1);
  const out: number[] = [];
  let e = NaN;
  for (const v of values) {
    e = isFinite(e) ? v * k + e * (1 - k) : v;
    out.push(e);
  }
  return out;
}

/** Wilder ADX with +DI / −DI. */
export function adx(c: Candle[], n = 14): { adx: number[]; pdi: number[]; mdi: number[] } {
  const A: number[] = [], P: number[] = [], M: number[] = [];
  let atr = NaN, pdm = NaN, mdm = NaN, ax = NaN;
  for (let i = 0; i < c.length; i++) {
    if (i === 0) {
      A.push(NaN), P.push(NaN), M.push(NaN);
      continue;
    }
    const up = c[i].h - c[i - 1].h;
    const dn = c[i - 1].l - c[i].l;
    const p = up > dn && up > 0 ? up : 0;
    const m = dn > up && dn > 0 ? dn : 0;
    const t = tr(c[i], c[i - 1].c);
    atr = isFinite(atr) ? atr - atr / n + t : t;
    pdm = isFinite(pdm) ? pdm - pdm / n + p : p;
    mdm = isFinite(mdm) ? mdm - mdm / n + m : m;
    const pdi = atr > 0 ? (100 * pdm) / atr : 0;
    const mdi = atr > 0 ? (100 * mdm) / atr : 0;
    const dx = pdi + mdi > 0 ? (100 * Math.abs(pdi - mdi)) / (pdi + mdi) : 0;
    ax = isFinite(ax) ? (ax * (n - 1) + dx) / n : dx;
    A.push(i >= 2 * n ? ax : NaN), P.push(pdi), M.push(mdi);
  }
  return { adx: A, pdi: P, mdi: M };
}

/** Wilder RSI of an arbitrary series. */
export function rsi(x: number[], n: number): number[] {
  const out: number[] = [];
  let g = NaN, l = NaN;
  for (let i = 0; i < x.length; i++) {
    if (i === 0) {
      out.push(NaN);
      continue;
    }
    const d = x[i] - x[i - 1];
    const up = Math.max(0, d), dn = Math.max(0, -d);
    g = isFinite(g) ? (g * (n - 1) + up) / n : up;
    l = isFinite(l) ? (l * (n - 1) + dn) / n : dn;
    out.push(i < n ? NaN : l === 0 ? 100 : 100 - 100 / (1 + g / l));
  }
  return out;
}

/** LBR/RSI: 3-period RSI of the 1-day change (Street Smarts, Momentum Pinball). */
export function lbrRsi(closes: number[]): number {
  const roc = closes.slice(1).map((c, i) => c - closes[i]);
  const r = rsi(roc, 3);
  return r[r.length - 1];
}

export class RaschkeEngine {
  readonly bars: Candle[] = [];
  readonly daily: Candle[];
  readonly setups: Setup[] = [];
  levels: DailyLevel[] = [];
  private curDay: Candle | null = null;
  private curDayComplete = false;
  private h4: Candle[] = [];
  private curH4: Candle | null = null;
  private atrD = NaN;
  private pending: Pending[] = [];
  private open: Open[] = [];
  private dayIndex = 0; // count of UTC days seen by step()
  private today = { armed: new Set<string>(), firstHour: null as { h: number; l: number } | null };

  constructor(
    readonly meta: { symbol: string; exchange: string; tick: number },
    dailyBefore: readonly Candle[],
    readonly p: RaschkeParams = BOOK_RASCHKE_PARAMS,
  ) {
    this.daily = [...dailyBefore];
    this.recomputeAtr();
  }

  get atrDaily(): number {
    return this.atrD;
  }
  activeLevels(): DailyLevel[] {
    return [];
  }
  stateOf(_levelId: string): LevelState {
    return 'WATCHING';
  }

  private on(s: RaschkeSetup): boolean {
    return this.p.setups.includes(s);
  }

  step(b: Candle): Setup[] {
    const newDay = this.rollDaily(b);
    this.rollH4(b);
    this.bars.push(b);
    if (this.bars.length > 3000) this.bars.shift();
    if (!isFinite(this.atrD) || this.daily.length < this.p.lookback + 5) return [];
    if (newDay) this.startOfDay(b);
    // 1) orders placed on earlier closes fill (or expire) on this bar
    const filled = this.fill(b);
    // 2) open trades
    this.manage(b, filled);
    // 3) new orders from this closed bar
    this.detect(b);
    return filled;
  }

  // ---------------- candles ----------------

  /** Returns true when b is the first bar of a new UTC day. */
  private rollDaily(b: Candle): boolean {
    const day = Math.floor(b.t / DAY) * DAY;
    let fresh = false;
    if (this.curDay && this.curDay.t !== day) {
      if (this.curDayComplete) {
        this.daily.push(this.curDay);
        this.recomputeAtr();
        this.dayClosed(this.curDay);
      }
      this.curDay = null;
    }
    if (!this.curDay) {
      this.curDay = { t: day, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, bv: 0 };
      const lastD = this.daily[this.daily.length - 1];
      this.curDayComplete = b.t === day && !(lastD && lastD.t >= day);
      this.dayIndex++;
      fresh = true;
    } else {
      this.curDay.h = Math.max(this.curDay.h, b.h);
      this.curDay.l = Math.min(this.curDay.l, b.l);
      this.curDay.c = b.c;
      this.curDay.v += b.v;
    }
    return fresh;
  }

  private rollH4(b: Candle): void {
    const t = Math.floor(b.t / H4) * H4;
    if (this.curH4 && this.curH4.t !== t) {
      this.h4.push(this.curH4);
      if (this.h4.length > 400) this.h4.shift();
      this.curH4 = null;
    }
    if (!this.curH4) this.curH4 = { t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, bv: 0 };
    else {
      this.curH4.h = Math.max(this.curH4.h, b.h);
      this.curH4.l = Math.min(this.curH4.l, b.l);
      this.curH4.c = b.c;
      this.curH4.v += b.v;
    }
    if (b.t + this.p.ltfMs >= t + H4) {
      this.h4.push(this.curH4);
      if (this.h4.length > 400) this.h4.shift();
      this.curH4 = null;
      if (this.p.hgTf === 'H4' && this.on('HOLY_GRAIL')) this.holyGrail(this.h4, b.t + this.p.ltfMs, H4);
    }
  }

  private recomputeAtr(): void {
    const d = this.daily.slice(-60);
    let a = NaN;
    for (let k = 1; k < d.length; k++) a = isFinite(a) ? (a * 13 + tr(d[k], d[k - 1].c)) / 14 : tr(d[k], d[k - 1].c);
    this.atrD = a;
  }

  private trendOk(dir: 1 | -1): boolean {
    if (this.p.trendFilter === 'none') return true;
    const d = this.daily;
    if (d.length < 55) return true;
    const sma = (end: number) => d.slice(end - 50, end).reduce((s, x) => s + x.c, 0) / 50;
    const now = sma(d.length), before = sma(d.length - 5), c = d[d.length - 1].c;
    const t = c > now && now > before ? 1 : c < now && now < before ? -1 : 0;
    return t !== -dir;
  }

  // ---------------- setups ----------------

  /** Called once when a UTC day has closed (its candle is in this.daily). Daily Holy Grail runs here. */
  private dayClosed(_d: Candle): void {
    if (this.p.hgTf === 'D1' && this.on('HOLY_GRAIL')) this.holyGrail(this.daily.slice(-300), _d.t + DAY, DAY);
  }

  /** First bar of a new UTC day: arm the setups that depend on closed days. */
  private startOfDay(b: Candle): void {
    const d = this.daily;
    const n = d.length;
    const y = d[n - 1];
    const dayEnd = Math.floor(b.t / DAY) * DAY + DAY;
    this.today = { armed: new Set(), firstHour: null };
    const A = this.atrD;
    // TURTLE SOUP +1: yesterday made a new 20-day low (vs the 20 days before it) with the prior low ≥ 3 sessions
    // before yesterday, and closed at or below that prior low → buy stop at the prior low today
    if (this.on('TURTLE_SOUP_PLUS_ONE') && n > this.p.lookback + 1) {
      const win = d.slice(n - 1 - this.p.lookback, n - 1);
      for (const dir of [1, -1] as const) {
        const ext = dir > 0 ? Math.min(...win.map((x) => x.l)) : Math.max(...win.map((x) => x.h));
        const idx = win.findIndex((x) => (dir > 0 ? x.l : x.h) === ext);
        const age = win.length - idx; // sessions before yesterday
        const newExt = dir > 0 ? y.l < ext : y.h > ext;
        const closedBeyond = dir > 0 ? y.c <= ext : y.c >= ext;
        if (newExt && closedBeyond && age >= this.p.tsPlusOneMinAge && this.trendOk(dir))
          this.pending.push({ setup: 'TURTLE_SOUP_PLUS_ONE', dir, price: ext, sl: (lo, hi) => (dir > 0 ? Math.min(lo, y.l) : Math.max(hi, y.h)), target: null, level: ext, expires: dayEnd, why: `вчера новый ${this.p.lookback}-дневный ${dir > 0 ? 'минимум' : 'максимум'} и закрытие за прежним (${age} сессий назад); стоп-ордер на прежнем экстремуме`, createdAt: b.t });
      }
    }
    // 80-20: yesterday opened in one 20 % extreme of its range and closed in the other
    if (this.on('EIGHTY_TWENTY')) {
      const r = y.h - y.l;
      if (r > 0) {
        if ((y.o - y.l) / r >= 0.8 && (y.c - y.l) / r <= 0.2) this.today.armed.add('8020:1');
        if ((y.o - y.l) / r <= 0.2 && (y.c - y.l) / r >= 0.8) this.today.armed.add('8020:-1');
      }
    }
    // MOMENTUM PINBALL: LBR/RSI at yesterday's close
    if (this.on('MOMENTUM_PINBALL')) {
      const v = lbrRsi(d.slice(-40).map((x) => x.c));
      if (v < this.p.pinballLow) this.today.armed.add('pinball:1');
      if (v > this.p.pinballHigh) this.today.armed.add('pinball:-1');
    }
    // ID/NR4: yesterday inside day and the narrowest range of the last 4 days → OCO stops around it
    if (this.on('ID_NR4') && n >= 5) {
      const inside = y.h <= d[n - 2].h && y.l >= d[n - 2].l;
      const nr4 = d.slice(n - 4).every((x) => y.h - y.l <= x.h - x.l);
      if (inside && nr4) {
        const off = this.p.offsetAtr * A;
        const oco = `idnr4:${y.t}`;
        if (this.trendOk(1)) this.pending.push({ setup: 'ID_NR4', dir: 1, price: y.h + off, sl: y.l, target: null, level: y.h, expires: dayEnd, oco, why: 'вчера внутренний день и самый узкий диапазон за 4 дня; стоп-ордер выше его максимума', createdAt: b.t });
        if (this.trendOk(-1)) this.pending.push({ setup: 'ID_NR4', dir: -1, price: y.l - off, sl: y.h, target: null, level: y.l, expires: dayEnd, oco, why: 'вчера внутренний день и самый узкий диапазон за 4 дня; стоп-ордер ниже его минимума', createdAt: b.t });
      }
    }
  }

  /** On every closed 15m bar: intraday triggers (Turtle Soup, 80-20 test, Pinball first hour). */
  private detect(b: Candle): void {
    const d = this.daily;
    const n = d.length;
    const day = this.curDay!;
    const dayEnd = day.t + DAY;
    const A = this.atrD;
    const y = d[n - 1];
    const has = (s: RaschkeSetup, dir: number) => this.pending.some((o) => o.setup === s && o.dir === dir && o.expires === dayEnd) || this.today.armed.has(`done:${s}:${dir}`);
    // TURTLE SOUP: today trades beyond the prior 20-day extreme made ≥ 4 sessions ago
    if (this.on('TURTLE_SOUP')) {
      const win = d.slice(n - this.p.lookback);
      for (const dir of [1, -1] as const) {
        if (has('TURTLE_SOUP', dir)) continue;
        const ext = dir > 0 ? Math.min(...win.map((x) => x.l)) : Math.max(...win.map((x) => x.h));
        const idx = win.findIndex((x) => (dir > 0 ? x.l : x.h) === ext);
        const age = win.length - idx; // 1 = yesterday
        const pierced = dir > 0 ? day.l < ext : day.h > ext;
        if (pierced && age >= this.p.tsMinAge && this.trendOk(dir)) {
          const off = this.p.offsetAtr * A;
          this.pending.push({ setup: 'TURTLE_SOUP', dir, price: ext + dir * off, sl: (lo, hi) => (dir > 0 ? lo : hi), target: null, level: ext, expires: dayEnd, why: `новый ${this.p.lookback}-дневный ${dir > 0 ? 'минимум' : 'максимум'}, прежний был ${age} сессий назад; стоп-ордер обратно за прежний экстремум`, createdAt: b.t });
          this.today.armed.add(`done:TURTLE_SOUP:${dir}`);
        }
      }
    }
    // 80-20: price first trades beyond yesterday's extreme, then a stop order back at it
    for (const dir of [1, -1] as const) {
      if (!this.today.armed.has(`8020:${dir}`) || has('EIGHTY_TWENTY', dir)) continue;
      const tested = dir > 0 ? day.l <= y.l - this.p.eightyTwentyTestAtr * A : day.h >= y.h + this.p.eightyTwentyTestAtr * A;
      if (tested && this.trendOk(dir)) {
        this.pending.push({ setup: 'EIGHTY_TWENTY', dir, price: dir > 0 ? y.l : y.h, sl: (lo, hi) => (dir > 0 ? lo : hi), target: null, level: dir > 0 ? y.l : y.h, expires: dayEnd, why: `вчера открытие в ${dir > 0 ? 'верхних' : 'нижних'} 20 % диапазона и закрытие в ${dir > 0 ? 'нижних' : 'верхних'} 20 %; сегодня тест за вчерашний ${dir > 0 ? 'минимум' : 'максимум'} и стоп-ордер обратно`, createdAt: b.t });
        this.today.armed.add(`done:EIGHTY_TWENTY:${dir}`);
      }
    }
    // MOMENTUM PINBALL: first hour of the UTC day (four 15m bars) sets the range
    if (b.t + this.p.ltfMs === day.t + 3_600_000) {
      const fh = this.bars.slice(-4);
      this.today.firstHour = { h: Math.max(...fh.map((x) => x.h)), l: Math.min(...fh.map((x) => x.l)) };
      for (const dir of [1, -1] as const) {
        if (!this.today.armed.has(`pinball:${dir}`) || !this.trendOk(dir)) continue;
        const fr = this.today.firstHour;
        this.pending.push({ setup: 'MOMENTUM_PINBALL', dir, price: dir > 0 ? fr.h : fr.l, sl: dir > 0 ? fr.l : fr.h, target: null, level: dir > 0 ? fr.h : fr.l, expires: dayEnd, why: `LBR/RSI вчера ${dir > 0 ? '< ' + this.p.pinballLow : '> ' + this.p.pinballHigh}; стоп-ордер на ${dir > 0 ? 'максимуме' : 'минимуме'} первого часа`, createdAt: b.t });
      }
    }
  }

  /** Holy Grail on closed bars of its time frame: first pullback to the 20 EMA while ADX(14) > 30. */
  private holyGrail(c: Candle[], closeTime: number, tfMs: number): void {
    if (c.length < 60) return;
    const closes = c.map((x) => x.c);
    const e = ema(closes, 20);
    const { adx: ax, pdi, mdi } = adx(c, 14);
    const i = c.length - 1;
    const x = c[i];
    const strong = ax[i] > this.p.hgAdx;
    if (!strong) return;
    for (const dir of [1, -1] as const) {
      if (dir > 0 ? pdi[i] <= mdi[i] : mdi[i] <= pdi[i]) continue;
      const touch = dir > 0 ? x.l <= e[i] : x.h >= e[i];
      // "the first pullback": the previous 5 bars stayed on the trend side of the EMA
      const clean = c.slice(-6, -1).every((z, k) => (dir > 0 ? z.l > e[i - 5 + k] : z.h < e[i - 5 + k]));
      if (!touch || !clean || !this.trendOk(dir)) continue;
      const swing = c.slice(-21, -1);
      const target = dir > 0 ? Math.max(...swing.map((z) => z.h)) : Math.min(...swing.map((z) => z.l));
      const entry = dir > 0 ? x.h + this.p.offsetAtr * this.atrD : x.l - this.p.offsetAtr * this.atrD;
      if (dir > 0 ? target <= entry : target >= entry) continue;
      this.pending = this.pending.filter((o) => !(o.setup === 'HOLY_GRAIL' && o.dir === dir));
      this.pending.push({ setup: 'HOLY_GRAIL', dir, price: entry, sl: (lo, hi) => (dir > 0 ? Math.min(lo, x.l) : Math.max(hi, x.h)), target, level: e[i], expires: closeTime + this.p.hgValidBars * tfMs, why: `ADX(14) ${ax[i].toFixed(0)} > ${this.p.hgAdx}, первый откат к EMA20 (${this.p.hgTf}); стоп-ордер ${dir > 0 ? 'выше максимума' : 'ниже минимума'} бара отката, цель — прежний ${dir > 0 ? 'максимум' : 'минимум'}`, createdAt: closeTime - this.p.ltfMs });
    }
  }

  // ---------------- orders and trades ----------------

  private fill(b: Candle): Setup[] {
    const out: Setup[] = [];
    const keep: Pending[] = [];
    const firedOco = new Set<string>();
    for (const o of this.pending) {
      if (b.t <= o.createdAt) {
        keep.push(o);
        continue;
      }
      if (b.t >= o.expires || (o.oco && firedOco.has(o.oco))) continue;
      const hit = o.dir > 0 ? b.h >= o.price : b.l <= o.price;
      const busy = this.open.some((x) => x._setup === o.setup);
      if (hit && !busy) {
        const px = o.dir > 0 ? Math.max(o.price, b.o) : Math.min(o.price, b.o); // a gap through the stop fills at the open
        const s = this.openTrade(o, b, px);
        if (s) out.push(s);
        if (o.oco) firedOco.add(o.oco);
        continue;
      }
      if (hit) continue; // missed: one position per setup
      keep.push(o);
    }
    this.pending = keep.filter((o) => !(o.oco && firedOco.has(o.oco)));
    return out;
  }

  private openTrade(o: Pending, b: Candle, px: number): Setup | null {
    const p = this.p;
    const day = this.curDay!;
    // the day's extreme known when the order fills: earlier bars of the day and the fill bar's open (the fill
    // bar's own low / high is only known at its close; it is checked against the stop right after)
    const earlier = this.bars.filter((x) => x.t >= day.t && x.t < b.t);
    const dayLo = Math.min(b.o, ...earlier.map((x) => x.l));
    const dayHi = Math.max(b.o, ...earlier.map((x) => x.h));
    let sl = typeof o.sl === 'number' ? o.sl : o.sl(dayLo, dayHi);
    sl -= o.dir * p.offsetAtr * 0.5 * this.atrD; // "one tick under": a small buffer
    if (p.minRiskAtr > 0 && Math.abs(px - sl) < p.minRiskAtr * this.atrD) sl = px - o.dir * p.minRiskAtr * this.atrD;
    const risk = (px - sl) * o.dir;
    if (!(risk > 0)) return null;
    const tp = o.target ?? (isFinite(p.targetR) ? px + o.dir * p.targetR * risk : null);
    const rr = tp !== null ? Math.abs(tp - px) / risk : NaN;
    const id = `${this.meta.symbol}:${o.setup}:${b.t}:${o.dir}`;
    const s: Open = {
      id,
      levelId: `${o.setup}:${day.t}`,
      symbol: this.meta.symbol,
      exchange: this.meta.exchange,
      t: b.t,
      confirmedAt: b.t,
      direction: o.dir > 0 ? 'LONG' : 'SHORT',
      level: o.level,
      levelStatus: '',
      levelStrength: 0,
      setupQuality: 50,
      distanceToLevelAtr: Math.abs(px - o.level) / this.atrD,
      approachBars: Math.round((b.t - o.createdAt) / p.ltfMs),
      volumeDecay: NaN,
      volumeSlope: 0,
      approach: { ...NO_FEAT },
      volatilityContraction: NaN,
      reactionVolumeRatio: NaN,
      reactionVolumeZ: NaN,
      reactionStrengthAtr: NaN,
      sweep: o.setup === 'TURTLE_SOUP' || o.setup === 'TURTLE_SOUP_PLUS_ONE' || o.setup === 'EIGHTY_TWENTY',
      microBos: false,
      trigger: `${NAMES[o.setup]} (Raschke): ${o.why}`,
      entry: px,
      invalidation: sl,
      sl,
      tp: tp ?? NaN,
      nextLevel: null,
      rr,
      regime: 'n/a',
      reasons: ['RASCHKE', o.setup, `HOLD_${p.holdDays}D`, p.trailPrevDay ? 'TRAIL_PREV_DAY' : 'NO_TRAIL'],
      qualityBreakdown: {},
      levelWhy: `${NAMES[o.setup]} — Street Smarts (Raschke & Connors)`,
      outcome: { status: 'open', r: 0, mfeR: 0, maeR: 0, barsToReaction: null, barsToInvalidation: null } as SetupOutcome,
      _sl: sl,
      _entryFee: p.feeTaker,
      _entryDay: day.t,
      _setup: o.setup,
      _target: tp,
    };
    this.setups.push(s);
    this.open.push(s);
    return s;
  }

  private costR(s: Open, exitFee: number, slip: number): number {
    return ((s._entryFee + exitFee + slip) * s.entry) / Math.abs(s.entry - s.sl);
  }

  private manage(b: Candle, fresh: Setup[]): void {
    const p = this.p;
    const still: Open[] = [];
    const dayEndBar = b.t + p.ltfMs >= Math.floor(b.t / DAY) * DAY + DAY; // last 15m bar of the UTC day
    for (const s of this.open) {
      const o = s.outcome;
      const dir = s.direction === 'LONG' ? 1 : -1;
      const risk = Math.abs(s.entry - s.sl);
      const isFresh = fresh.includes(s);
      const fav = (dir > 0 ? b.h - s.entry : s.entry - b.l) / risk;
      const adv = (dir > 0 ? s.entry - b.l : b.h - s.entry) / risk;
      if (!isFresh) o.mfeR = Math.max(o.mfeR, fav);
      o.maeR = Math.max(o.maeR, Math.max(0, adv));
      const close = (px: number, fee: number, slip: number, status?: SetupOutcome['status']) => {
        const gross = (dir > 0 ? px - s.entry : s.entry - px) / risk;
        o.r = gross - this.costR(s, fee, slip);
        o.status = status ?? (gross > 1e-9 ? 'win' : gross < -1e-9 ? 'loss' : 'timeout');
        o.closedAt = b.t;
      };
      if (dir > 0 ? b.l <= s._sl : b.h >= s._sl) {
        close(s._sl, p.feeTaker, p.slippage);
        o.barsToInvalidation = Math.round((b.t - s.t) / p.ltfMs) + 1;
        continue;
      }
      if (!isFresh && s._target !== null && (dir > 0 ? b.h >= s._target : b.l <= s._target)) {
        close(s._target, p.feeMaker, 0, 'win');
        continue;
      }
      if (dayEndBar) {
        const daysHeld = Math.round((Math.floor(b.t / DAY) * DAY - s._entryDay) / DAY) + 1;
        const inProfit = dir > 0 ? b.c > s.entry : b.c < s.entry;
        const quick = p.exitIfNotProfitableAtClose && (s._setup === 'MOMENTUM_PINBALL' || s._setup === 'ID_NR4') && daysHeld === 1 && !inProfit;
        if (daysHeld >= p.holdDays || quick) {
          close(b.c, p.feeTaker, 0, 'timeout');
          if (o.r > 0) o.status = 'win';
          continue;
        }
        // trailing: once a day has closed in profit, the stop follows that day's low (high)
        const d = this.curDay!;
        if (p.trailPrevDay && inProfit) {
          const trail = dir > 0 ? d.l : d.h;
          if (dir > 0 ? trail > s._sl : trail < s._sl) s._sl = trail;
        }
      }
      o.r = (dir > 0 ? b.c - s.entry : s.entry - b.c) / risk;
      still.push(s);
    }
    this.open = still;
  }
}
