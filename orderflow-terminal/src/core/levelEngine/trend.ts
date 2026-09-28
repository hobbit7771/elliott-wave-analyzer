// Trend model: a Donchian channel breakout ("turtle" system / time-series momentum), executed on the same
// closed 15m bars as the other engines; days are UTC days.
//
// Why (measured on 17 Bybit perpetuals, daily 2021–2026, see README): daily returns are close to a random walk
// (autocorrelation ≈ 0, variance ratios ≈ 1), volatility clusters strongly, returns have fat tails, and the
// coins move together (mean correlation 0.6 — the market factor). A breakout system with a volatility-scaled
// stop loses small and often but keeps the rare long trends of the fat right tail; the stop in ATR makes the
// position size inversely proportional to volatility. Shorts did not pay (squeezes, upward drift of listed
// coins), and a filter on the market factor (BTC's D1 trend) improved the result.
//
// Rules: at the start of each UTC day, if the last close is below the highest high of the last nIn closed days,
// a buy stop at that high is valid for the day. Initial stop = entry − stopAtr × ATR(20, D1); the stop trails to
// the lowest low of the last nOut closed days (updated at each day close). Skip when BTC's D1 trend is down
// (close below a falling SMA50), and skip when the coin's perpetual funding over the last 7 days averaged more
// than maxFundingPerDay (crowded longs: on 17 coins 2021–2026 such breakouts lost −0.24R on average against
// +0.53R for the rest, and the cross-section of funding predicts lower returns). Stop first when a bar reaches both; Bybit fees, stop slippage and an estimate of
// perpetual funding for longs are charged in R.
import type { Candle } from '../types.js';
import type { DailyLevel } from './dailyLevels.js';
import type { Feat, LevelState, OpenPosition, Setup, SetupOutcome, WorkingOrder } from './setupEngine.js';

export interface TrendParams {
  model: 'trend';
  ltfMs: number;
  nIn: number;
  nOut: number;
  stopAtr: number;
  longOnly: boolean;
  marketFilter: boolean; // BTC D1 trend must not be down (longs) / up (shorts)
  marketSma: number;
  feeTaker: number;
  slippage: number;
  fundingPerDay: number; // charged to longs (estimate: 0.01 % per 8 h)
  /** skip longs (shorts: below −value) when the mean daily funding of the last 7 days exceeds it; 0 = off */
  maxFundingPerDay: number;
}

/** One perpetual funding settlement: time (ms) and rate (fraction, per settlement). */
export interface Funding {
  t: number;
  rate: number;
}

export const SITE_TREND_PARAMS: TrendParams = {
  model: 'trend',
  ltfMs: 15 * 60_000,
  nIn: 20,
  nOut: 10,
  stopAtr: 3,
  longOnly: true,
  marketFilter: true,
  marketSma: 50,
  feeTaker: 0.00055,
  slippage: 0.0002,
  fundingPerDay: 0.0003,
  maxFundingPerDay: 0.0005,
};

/** Mean funding per day over the 7 days before `day` (settlements in [day − 7 d, day)); NaN with < 4 days of data. */
export function fundingPerDay7(f: readonly Funding[], day: number): number {
  let sum = 0;
  let n = 0;
  const days = new Set<number>();
  for (const x of f) if (x.t >= day - 7 * DAY && x.t < day) {
    sum += x.rate;
    n++;
    days.add(Math.floor(x.t / DAY));
  }
  return n && days.size >= 4 ? sum / 7 : NaN;
}

const DAY = 86_400_000;
const NO_FEAT: Feat = { dist: NaN, volDecay: NaN, volSlope: 0, contraction: NaN, impulse: NaN, candleRangeAtr: NaN, approachVelocity: 0, pullbackDepth: NaN, barsToLevel: NaN };
const tr = (b: Candle, pc: number) => Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc));

/** +1 up, −1 down, 0 neither — from closed daily candles (close vs a rising / falling SMA). */
export function dailyTrend(d: readonly Candle[], n = 50): 1 | -1 | 0 {
  if (d.length < n + 6) return 0;
  const sma = (end: number) => d.slice(end - n, end).reduce((s, x) => s + x.c, 0) / n;
  const now = sma(d.length), before = sma(d.length - 5), c = d[d.length - 1].c;
  if (c > now && now > before) return 1;
  if (c < now && now < before) return -1;
  return 0;
}

type Open = Setup & { _sl: number; _entryDay: number };

export class TrendEngine {
  readonly bars: Candle[] = [];
  readonly daily: Candle[];
  readonly setups: Setup[] = [];
  levels: DailyLevel[] = [];
  private curDay: Candle | null = null;
  private curDayComplete = false;
  private atr20 = NaN;
  private order: { price: number; stop: number; expires: number; dir: 1 | -1; createdAt: number; channel: number } | null = null;
  private open: Open | null = null;
  /** closed daily candles of the market factor (BTC); only candles before the current day are used */
  private market: Candle[] = [];
  /** funding settlements of this coin (empty = the funding filter is off) */
  private funding: Funding[] = [];
  /** why no order was placed today (shown in the entries panel) */
  skipped = '';
  /** a serious negative news event for this coin (news filter): no new longs while set */
  private eventRisk = '';

  constructor(
    readonly meta: { symbol: string; exchange: string; tick: number },
    dailyBefore: readonly Candle[],
    readonly p: TrendParams = SITE_TREND_PARAMS,
    market: readonly Candle[] = [],
    funding: readonly Funding[] = [],
  ) {
    this.daily = [...dailyBefore];
    this.market = [...market];
    this.funding = [...funding];
    this.recompute();
  }

  /** Live: replace the market-factor daily candles (BTC) when a new day has closed. */
  setMarket(d: readonly Candle[]): void {
    this.market = [...d];
  }

  /** News filter: set (reason) or clear ('') the event risk; an active long order is cancelled. */
  setEventRisk(reason: string): void {
    this.eventRisk = reason;
    if (reason && this.order && this.order.dir > 0 && !this.open) {
      this.order = null;
      this.skipped = `событие: ${reason}`;
    }
  }

  /** Live: replace the coin's funding history (refreshed a few times a day). */
  setFunding(f: readonly Funding[]): void {
    this.funding = [...f];
  }

  get atrDaily(): number {
    return this.atr20;
  }

  workingOrders(): WorkingOrder[] {
    const o = this.order;
    if (!o || this.open) return [];
    return [{ model: 'TREND_BREAKOUT', dir: o.dir, kind: 'stop', price: o.price, sl: o.stop, why: `пробой ${this.p.nIn}-дневного ${o.dir > 0 ? 'максимума' : 'минимума'} (стоп-ордер на сегодня, UTC)` }];
  }

  openPositions(): OpenPosition[] {
    return this.open ? [{ setup: this.open, stop: this.open._sl }] : [];
  }
  activeLevels(): DailyLevel[] {
    return [];
  }
  stateOf(_id: string): LevelState {
    return 'WATCHING';
  }

  step(b: Candle): Setup[] {
    const fresh = this.rollDaily(b);
    this.bars.push(b);
    if (this.bars.length > 200) this.bars.shift();
    if (!isFinite(this.atr20) || this.daily.length < Math.max(this.p.nIn, this.p.nOut) + 2) return [];
    if (fresh) this.startOfDay(b);
    const out: Setup[] = [];
    const o = this.order;
    if (o && !this.open && b.t > o.createdAt) {
      if (b.t >= o.expires) this.order = null;
      else if (o.dir > 0 ? b.h >= o.price : b.l <= o.price) {
        const px = o.dir > 0 ? Math.max(o.price, b.o) : Math.min(o.price, b.o);
        out.push(this.enter(o, b, px));
        this.order = null;
      }
    }
    if (this.open) this.manage(b, out.length > 0);
    return out;
  }

  private rollDaily(b: Candle): boolean {
    const day = Math.floor(b.t / DAY) * DAY;
    let fresh = false;
    if (this.curDay && this.curDay.t !== day) {
      if (this.curDayComplete) {
        this.daily.push(this.curDay);
        if (this.daily.length > 400) this.daily.shift();
        this.recompute();
        this.dayClosed();
      }
      this.curDay = null;
    }
    if (!this.curDay) {
      this.curDay = { t: day, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, bv: 0 };
      const lastD = this.daily[this.daily.length - 1];
      this.curDayComplete = b.t === day && !(lastD && lastD.t >= day);
      fresh = true;
    } else {
      this.curDay.h = Math.max(this.curDay.h, b.h);
      this.curDay.l = Math.min(this.curDay.l, b.l);
      this.curDay.c = b.c;
      this.curDay.v += b.v;
    }
    return fresh;
  }

  private recompute(): void {
    const d = this.daily.slice(-80);
    let a = NaN;
    for (let k = 1; k < d.length; k++) a = isFinite(a) ? (a * 19 + tr(d[k], d[k - 1].c)) / 20 : tr(d[k], d[k - 1].c);
    this.atr20 = a;
  }

  private marketTrend(beforeDay: number): 1 | -1 | 0 {
    const m = this.market.filter((x) => x.t < beforeDay);
    return dailyTrend(m, this.p.marketSma);
  }

  /** A UTC day has closed: trail the open position's stop to the nOut-day extreme. */
  private dayClosed(): void {
    const s = this.open;
    if (!s) return;
    const w = this.daily.slice(-this.p.nOut);
    const dir = s.direction === 'LONG' ? 1 : -1;
    const trail = dir > 0 ? Math.min(...w.map((x) => x.l)) : Math.max(...w.map((x) => x.h));
    if (dir > 0 ? trail > s._sl : trail < s._sl) s._sl = trail;
  }

  private startOfDay(b: Candle): void {
    const p = this.p;
    const d = this.daily;
    const day = Math.floor(b.t / DAY) * DAY;
    this.order = null;
    this.skipped = '';
    if (this.open) return;
    const w = d.slice(-p.nIn);
    const last = d[d.length - 1];
    const mt = p.marketFilter ? this.marketTrend(day) : 0;
    for (const dir of [1, -1] as const) {
      if (dir < 0 && p.longOnly) continue;
      if (p.marketFilter && mt === -dir) {
        this.skipped = 'тренд BTC против';
        continue;
      }
      if (dir > 0 && this.eventRisk) {
        this.skipped = `событие: ${this.eventRisk}`;
        continue;
      }
      if (p.maxFundingPerDay > 0 && this.funding.length) {
        const f = fundingPerDay7(this.funding, day);
        if (dir > 0 ? f > p.maxFundingPerDay : f < -p.maxFundingPerDay) {
          this.skipped = `фандинг ${(f * 100).toFixed(3)} %/день за 7 дней — перегрев ${dir > 0 ? 'лонгов' : 'шортов'}`;
          continue;
        }
      }
      const ch = dir > 0 ? Math.max(...w.map((x) => x.h)) : Math.min(...w.map((x) => x.l));
      if (dir > 0 ? last.c >= ch : last.c <= ch) continue; // already beyond: no fresh breakout order
      this.skipped = '';
      this.order = { price: ch, stop: ch - dir * p.stopAtr * this.atr20, expires: day + DAY, dir, createdAt: b.t - 1, channel: ch };
      return;
    }
  }

  private enter(o: NonNullable<TrendEngine['order']>, b: Candle, px: number): Setup {
    const p = this.p;
    const sl = o.stop;
    const day = Math.floor(b.t / DAY) * DAY;
    const s: Open = {
      id: `${this.meta.symbol}:TREND:${b.t}`,
      levelId: `TREND:${day}`,
      symbol: this.meta.symbol,
      exchange: this.meta.exchange,
      t: b.t,
      confirmedAt: b.t,
      direction: o.dir > 0 ? 'LONG' : 'SHORT',
      level: o.channel,
      levelStatus: '',
      levelStrength: 0,
      setupQuality: 50,
      distanceToLevelAtr: Math.abs(px - o.channel) / this.atr20,
      approachBars: 0,
      volumeDecay: NaN,
      volumeSlope: 0,
      approach: { ...NO_FEAT },
      volatilityContraction: NaN,
      reactionVolumeRatio: NaN,
      reactionVolumeZ: NaN,
      reactionStrengthAtr: NaN,
      sweep: false,
      microBos: false,
      trigger: `Тренд (пробой канала Дончиана): пробой ${p.nIn}-дневного ${o.dir > 0 ? 'максимума' : 'минимума'}, стоп ${p.stopAtr} ATR(20), выход по ${p.nOut}-дневному ${o.dir > 0 ? 'минимуму' : 'максимуму'}${p.marketFilter ? ', тренд BTC не против' : ''}${p.maxFundingPerDay > 0 && this.funding.length ? `, фандинг за 7 дней ≤ ${(p.maxFundingPerDay * 100).toFixed(2)} %/день` : ''}`,
      entry: px,
      invalidation: sl,
      sl,
      tp: NaN,
      nextLevel: null,
      rr: NaN,
      regime: 'n/a',
      reasons: ['TREND', 'TREND_BREAKOUT', `DONCHIAN_${p.nIn}_${p.nOut}`, `STOP_${p.stopAtr}ATR`],
      qualityBreakdown: {},
      levelWhy: `канал Дончиана ${p.nIn} дней (система «черепах», тайм-серийный моментум)`,
      outcome: { status: 'open', r: 0, mfeR: 0, maeR: 0, barsToReaction: null, barsToInvalidation: null } as SetupOutcome,
      _sl: sl,
      _entryDay: day,
    };
    this.setups.push(s);
    this.open = s;
    return s;
  }

  private manage(b: Candle, fresh: boolean): void {
    const s = this.open!;
    const p = this.p;
    const o = s.outcome;
    const dir = s.direction === 'LONG' ? 1 : -1;
    const risk = Math.abs(s.entry - s.sl);
    const fav = (dir > 0 ? b.h - s.entry : s.entry - b.l) / risk;
    const adv = (dir > 0 ? s.entry - b.l : b.h - s.entry) / risk;
    if (!fresh) o.mfeR = Math.max(o.mfeR, fav);
    o.maeR = Math.max(o.maeR, Math.max(0, adv));
    const days = Math.max(0, Math.round((Math.floor(b.t / DAY) * DAY - s._entryDay) / DAY));
    const funding = dir > 0 ? (p.fundingPerDay * days * s.entry) / risk : 0;
    if (dir > 0 ? b.l <= s._sl : b.h >= s._sl) {
      // a gap through the stop (bar opens beyond it, not on the entry bar) fills at the open
      const px = !fresh && (dir > 0 ? b.o < s._sl : b.o > s._sl) ? b.o : s._sl;
      const gross = (dir > 0 ? px - s.entry : s.entry - px) / risk;
      o.r = gross - ((2 * p.feeTaker + p.slippage) * s.entry) / risk - funding;
      o.status = o.r > 0 ? 'win' : 'loss';
      o.closedAt = b.t;
      if (gross < 0) o.barsToInvalidation = Math.round((b.t - s.t) / p.ltfMs) + 1;
      this.open = null;
      return;
    }
    if (o.barsToReaction === null && !fresh && fav >= 1) o.barsToReaction = Math.round((b.t - s.t) / p.ltfMs) + 1;
    o.r = (dir > 0 ? b.c - s.entry : s.entry - b.c) / risk - funding;
  }
}
