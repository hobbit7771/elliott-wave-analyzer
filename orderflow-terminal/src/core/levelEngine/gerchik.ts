// Level trading after A. Gerchik's rules, on the same closed-bar stream as the other engine (HISTORICAL,
// REPLAY and LIVE share this code). Levels are the strong D1 levels; entries follow three models:
//
//  • BOUNCE (отбой, БСУ → БПУ1 → БПУ2): the D1 level is the БСУ. A closed bar that touches the level
//    "tick to tick" (within the luft) from the correct side and closes back on that side is БПУ1. A limit
//    order is then placed at level ± luft and fills when a later bar comes back to it (БПУ2).
//  • FALSE BREAKOUT (ложный пробой): a bar pierces the level by less than a stop and within a few bars a
//    bar closes back on the original side; entry at that close, stop beyond the false-break extreme.
//  • BREAKOUT WITH COMPRESSION (пробой с поджатием): several small bars pressed against the level with
//    rising lows (falling highs for a short); a stop order just beyond the level.
//
// Risk: stop S = stopAtr × ATR(D1) (Gerchik: 10–20 % of ATR), luft = 20 % of S, target = rr × S (≥ 3:1 in
// his rules); a trade is skipped when the next strong level leaves no room for the target, when the day has
// already moved ≥ atrExhaust × ATR in the trade direction ("ATR exhausted"), or against the D1 trend.
//
// No look-ahead: orders are created at the CLOSE of a bar and can only fill on later bars. If a bar reaches
// both the stop and the target, the stop is assumed first. Exchange fees are charged in R on every trade.
import type { Candle } from '../types.js';
import { analyzeDailyLevels, DEFAULT_DAILY_LEVEL_PARAMS, type DailyLevel, type DailyLevelParams } from './dailyLevels.js';
import type { Feat, LevelState, Setup, SetupOutcome } from './setupEngine.js';

export interface GerchikParams {
  model: 'gerchik';
  ltfMs: number;
  minStrength: number;
  dailyLookback: number;
  stopAtr: number; // stop S in ATR(D1)
  luftFrac: number; // luft as a fraction of S
  rr: number; // target in S
  bounce: boolean;
  falseBreak: boolean;
  breakout: boolean;
  bpuWindow: number; // bars a БПУ1 limit order stays valid
  cancelStops: number; // cancel a pending order when price runs this many S away from it
  trend: 'none' | 'd1' | 'strict';
  trendSma: number; // D1 SMA length for the trend
  atrExhaust: number; // skip when today's move in the trade direction ≥ this × ATR (Infinity: off)
  fbMaxDepth: number; // false breakout: max pierce beyond the level, in S
  fbReturnBars: number; // bars allowed to close back inside
  fbMaxRisk: number; // false breakout: max stop distance, in S
  squeezeBars: number; // breakout: bars pressed against the level
  squeezeRangeAtr: number; // breakout: max bar range in ATR(D1)
  roomCheck: boolean; // skip when the next strong level is closer than the target
  breakevenR: number; // move the stop to entry after +this R (Infinity: off)
  trailR: number; // after +trailR, trail the stop trailR behind the best close-to-date excursion (Infinity: off)
  cooldownBars: number; // per level after a trade
  maxOpen: number; // concurrent open trades per instrument
  maxHoldBars: number;
  /** level sources: strong historical D1 levels, and/or fresh БСУ levels (highs / lows of the last bsuDays
   *  closed daily candles that no later daily close has crossed) */
  levelSource: 'd1' | 'recent' | 'both';
  bsuDays: number;
  feeMaker: number;
  feeTaker: number;
  slippage: number; // on stop / market exits
}

export const DEFAULT_GERCHIK_PARAMS: GerchikParams = {
  model: 'gerchik',
  ltfMs: 15 * 60_000,
  minStrength: 55,
  dailyLookback: 365,
  stopAtr: 0.15,
  luftFrac: 0.2,
  rr: 3,
  bounce: true,
  falseBreak: true,
  breakout: false,
  bpuWindow: 16,
  cancelStops: 2,
  trend: 'd1',
  trendSma: 20,
  atrExhaust: 0.75,
  fbMaxDepth: 1,
  fbReturnBars: 2,
  fbMaxRisk: 1.5,
  squeezeBars: 4,
  squeezeRangeAtr: 0.12,
  roomCheck: true,
  breakevenR: Infinity,
  trailR: Infinity,
  cooldownBars: 16,
  maxOpen: 1,
  maxHoldBars: 480,
  levelSource: 'd1',
  bsuDays: 3,
  feeMaker: 0.0002, // Bybit linear, base tier
  feeTaker: 0.00055,
  slippage: 0.0002,
};

/**
 * The configuration the site runs for every coin. It was chosen once on 14 Bybit perpetuals (a year of 15m
 * bars, Bybit fees included) from the rule families that held up best — strongest D1 levels only, trades
 * only in the D1 trend, bounce and breakout-with-compression — and it is NOT re-tuned per coin: tuning per
 * coin on one period did not carry over to the next (TRAIN→VALIDATION correlation ≈ 0 over 800 variants).
 */
export const SITE_GERCHIK_PARAMS: GerchikParams = {
  ...DEFAULT_GERCHIK_PARAMS,
  levelSource: 'd1',
  minStrength: 70,
  trend: 'strict',
  trendSma: 50,
  bounce: true,
  falseBreak: false,
  breakout: true,
  stopAtr: 0.5,
  rr: 3,
  atrExhaust: Infinity,
  roomCheck: true,
  bpuWindow: 48,
};
export const SITE_GERCHIK_CHOICE =
  'Фиксированные параметры по правилам Герчика (уровни D1 силой ≥ 70, только по тренду D1, отбой БСУ/БПУ и пробой с поджатием, стоп 0,5 ATR(D1), цель 3:1), выбраны один раз на 14 монетах Bybit; под монету не подгоняются. Комиссии Bybit включены в R.';

type Model = 'BOUNCE' | 'FALSE_BREAK' | 'BREAKOUT';

interface Pending {
  levelId: string;
  model: Model;
  dir: 1 | -1;
  kind: 'limit' | 'stop';
  price: number;
  sl: number;
  risk: number;
  createdBar: number;
  expiresBar: number;
  trigger: string;
}

interface Track {
  prevSide: 1 | -1 | 0; // side of the previous close vs the level (+1 above)
  pierce?: { dir: 1 | -1; bar: number; extreme: number }; // false-breakout candidate
  cooldownUntil: number;
  state: LevelState;
}

const DAY = 86_400_000;
const NO_FEAT: Feat = { dist: NaN, volDecay: NaN, volSlope: 0, contraction: NaN, impulse: NaN, candleRangeAtr: NaN, approachVelocity: 0, pullbackDepth: NaN, barsToLevel: NaN };

const tr = (b: Candle, pc: number) => Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc));

export class GerchikEngine {
  readonly bars: Candle[] = [];
  readonly daily: Candle[];
  levels: DailyLevel[] = [];
  readonly setups: Setup[] = [];
  private tracks = new Map<string, Track>();
  private pending: Pending[] = [];
  private open: (Setup & { _sl: number; _entryFee: number })[] = [];
  private curDay: Candle | null = null;
  private curDayComplete = false;
  private atrD = NaN;
  private i = -1;

  constructor(
    readonly meta: { symbol: string; exchange: string; tick: number },
    dailyBefore: readonly Candle[],
    readonly p: GerchikParams = DEFAULT_GERCHIK_PARAMS,
    readonly lp: DailyLevelParams = DEFAULT_DAILY_LEVEL_PARAMS,
    private readonly maxBars = 3000,
    /** optional memo of the D1 level analysis (it depends only on the daily candles): used by batch experiments */
    private readonly levelCache?: Map<string, { levels: DailyLevel[]; atr: number }>,
  ) {
    this.daily = [...dailyBefore];
    this.recomputeLevels();
    this.recomputeRecent();
  }

  activeLevels(): DailyLevel[] {
    const d1 = this.p.levelSource === 'recent' ? [] : this.levels.filter((l) => (l.status === 'STRONG' || l.status === 'FLIPPED') && l.strength >= this.p.minStrength);
    return this.p.levelSource === 'd1' ? d1 : [...d1, ...this.recent.filter((r) => !d1.some((l) => Math.abs(l.price - r.price) <= 0.1 * this.atrD))];
  }

  /** fresh БСУ levels: highs / lows of the last bsuDays closed daily candles not crossed by a later daily close */
  private recent: DailyLevel[] = [];
  private recomputeRecent(): void {
    const d = this.daily;
    const out: DailyLevel[] = [];
    for (let k = Math.max(0, d.length - this.p.bsuDays); k < d.length; k++) {
      const later = d.slice(k + 1);
      for (const [price, role] of [
        [d[k].h, 'resistance'],
        [d[k].l, 'support'],
      ] as const) {
        if (later.some((x) => (role === 'resistance' ? x.c > price : x.c < price))) continue;
        out.push({ id: `bsu:${d[k].t}:${role}`, price, zone: 0, role, status: 'STRONG', strength: 50, rejections: 1, cleanReactions: 0, sweepReclaims: 0, attempts: 1, volumeRel: 1, avgReactionAtr: 0, avgReactionDays: 0, chopScore: 0, lastTouch: d[k].t, breakdown: {}, why: `БСУ: ${role === 'resistance' ? 'максимум' : 'минимум'} дня ${new Date(d[k].t).toISOString().slice(0, 10)}` });
      }
    }
    this.recent = out;
  }

  stateOf(levelId: string): LevelState {
    if (this.pending.some((o) => o.levelId === levelId)) return 'TOUCH';
    if (this.open.some((o) => o.levelId === levelId)) return 'CONFIRMED';
    return this.tracks.get(levelId)?.state ?? 'WATCHING';
  }

  get atrDaily(): number {
    return this.atrD;
  }

  step(b: Candle): Setup[] {
    this.i++;
    this.rollDaily(b);
    this.bars.push(b);
    if (this.bars.length > this.maxBars) this.bars.shift();
    if (!isFinite(this.atrD)) return [];
    // 1) orders placed on earlier closes fill (or not) on this bar
    const filled = this.fillPending(b);
    // 2) open trades on this bar (a trade filled on this bar is only checked against its stop)
    this.updateOpen(b, filled);
    // 3) new orders from this closed bar
    for (const lv of this.activeLevels()) this.detect(lv, b);
    return filled;
  }

  // ---------------- daily candles, levels, ATR, trend ----------------

  private rollDaily(b: Candle): void {
    const day = Math.floor(b.t / DAY) * DAY;
    if (this.curDay && this.curDay.t !== day) {
      if (this.curDayComplete) {
        this.daily.push(this.curDay);
        this.recomputeLevels();
        this.recomputeRecent();
      }
      this.curDay = null;
    }
    if (!this.curDay) {
      this.curDay = { t: day, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, bv: 0 };
      const lastD = this.daily[this.daily.length - 1];
      this.curDayComplete = b.t === day && !(lastD && lastD.t >= day);
    } else {
      this.curDay.h = Math.max(this.curDay.h, b.h);
      this.curDay.l = Math.min(this.curDay.l, b.l);
      this.curDay.c = b.c;
      this.curDay.v += b.v;
    }
  }

  private recomputeLevels(): void {
    const d = this.daily.slice(-this.p.dailyLookback);
    const ck = `${this.meta.symbol}:${this.p.dailyLookback}:${d[d.length - 1]?.t ?? 0}:${d.length}`;
    const hit = this.levelCache?.get(ck);
    if (hit) {
      this.levels = hit.levels.map((l) => ({ ...l }));
      this.atrD = hit.atr;
      return;
    }
    const fresh = analyzeDailyLevels(d, this.lp, this.meta.tick);
    for (const l of fresh) {
      const old = this.levels.find((o) => Math.abs(o.price - l.price) <= Math.max(o.zone, l.zone) * 2);
      if (old) l.id = old.id;
    }
    this.levels = fresh;
    let a = NaN;
    for (let k = 1; k < d.length; k++) a = isFinite(a) ? (a * 13 + tr(d[k], d[k - 1].c)) / 14 : tr(d[k], d[k - 1].c);
    this.atrD = a;
    this.levelCache?.set(ck, { levels: fresh.map((l) => ({ ...l })), atr: a });
  }

  /** +1 uptrend, −1 downtrend, 0 range — closed D1 candles only. */
  private trend(): 1 | -1 | 0 {
    const d = this.daily;
    const n = this.p.trendSma;
    if (d.length < n + 5) return 0;
    const sma = (end: number) => d.slice(end - n, end).reduce((s, x) => s + x.c, 0) / n;
    const now = sma(d.length);
    const before = sma(d.length - 5);
    const c = d[d.length - 1].c;
    if (c > now && now > before) return 1;
    if (c < now && now < before) return -1;
    return 0;
  }

  private trendAllows(dir: 1 | -1): boolean {
    if (this.p.trend === 'none') return true;
    const t = this.trend();
    return this.p.trend === 'strict' ? t === dir : t !== -dir;
  }

  /** "ATR exhausted": today already moved ≥ atrExhaust × ATR in the trade direction. */
  private exhausted(dir: 1 | -1, price: number): boolean {
    if (!isFinite(this.p.atrExhaust) || !this.curDay) return false;
    const moved = dir > 0 ? price - this.curDay.l : this.curDay.h - price;
    return moved >= this.p.atrExhaust * this.atrD;
  }

  /** Room to the next strong level in the trade direction. */
  private roomOk(lv: DailyLevel, dir: 1 | -1, entry: number, target: number): { ok: boolean; next: number | null } {
    const next =
      this.activeLevels()
        .filter((l) => l.id !== lv.id && (dir > 0 ? l.price > entry : l.price < entry))
        .sort((a, b) => (dir > 0 ? a.price - b.price : b.price - a.price))[0]?.price ?? null;
    if (!this.p.roomCheck || next === null) return { ok: true, next };
    return { ok: dir > 0 ? next >= target : next <= target, next };
  }

  // ---------------- detection on a closed bar ----------------

  private detect(lv: DailyLevel, b: Candle): void {
    const p = this.p;
    let t = this.tracks.get(lv.id);
    if (!t) {
      t = { prevSide: 0, cooldownUntil: -1, state: 'WATCHING' };
      this.tracks.set(lv.id, t);
    }
    const S = p.stopAtr * this.atrD;
    const L = p.luftFrac * S;
    const lvl = lv.price;
    const prev = this.bars[this.bars.length - 2];
    const side: 1 | -1 = b.c >= lvl ? 1 : -1;
    const prevSide = t.prevSide;
    t.prevSide = side;
    if (!prev) return;
    const dist = Math.abs(b.c - lvl) / this.atrD;
    t.state = dist <= 1 ? 'APPROACHING' : 'WATCHING';
    if (this.i < t.cooldownUntil) return;
    if (this.pending.some((o) => o.levelId === lv.id) || this.open.some((o) => o.levelId === lv.id)) return;

    // ---- BOUNCE: БПУ1 from the correct side, closed back on that side ----
    if (p.bounce) {
      // support (price above): low within ±luft of the level, close above the level, previous close above level+luft
      for (const dir of [1, -1] as const) {
        const touch = dir > 0 ? b.l <= lvl + L && b.l >= lvl - L && b.c > lvl && prev.c > lvl + L : b.h >= lvl - L && b.h <= lvl + L && b.c < lvl && prev.c < lvl - L;
        if (!touch) continue;
        const entry = lvl + dir * L;
        const sl = entry - dir * S;
        if (this.place(lv, dir, 'BOUNCE', 'limit', entry, sl, p.bpuWindow, `БПУ1 ${dir > 0 ? 'снизу к поддержке' : 'сверху к сопротивлению'} «тик в тик» (люфт ${L.toPrecision(3)}), лимит на ${dir > 0 ? 'покупку' : 'продажу'} уровень ${dir > 0 ? '+' : '−'} люфт на БПУ2`)) {
          t.state = 'TOUCH';
          return;
        }
      }
    }

    // ---- FALSE BREAKOUT: pierce by < fbMaxDepth·S, then a close back on the original side ----
    if (p.falseBreak) {
      // pierce starts: previous close on one side, this bar's extreme through the level by more than the luft
      if (!t.pierce) {
        if (prevSide === 1 && b.l < lvl - L && lvl - b.l <= p.fbMaxDepth * S) t.pierce = { dir: 1, bar: this.i, extreme: b.l };
        else if (prevSide === -1 && b.h > lvl + L && b.h - lvl <= p.fbMaxDepth * S) t.pierce = { dir: -1, bar: this.i, extreme: b.h };
      } else {
        t.pierce.extreme = t.pierce.dir > 0 ? Math.min(t.pierce.extreme, b.l) : Math.max(t.pierce.extreme, b.h);
      }
      const pc = t.pierce;
      if (pc) {
        const depth = pc.dir > 0 ? lvl - pc.extreme : pc.extreme - lvl;
        if (depth > p.fbMaxDepth * S || this.i - pc.bar > p.fbReturnBars) t.pierce = undefined; // a real breakout or too slow
        else if (pc.dir > 0 ? b.c > lvl + L : b.c < lvl - L) {
          t.pierce = undefined;
          const entry = b.c;
          const sl = pc.extreme - pc.dir * L;
          if (Math.abs(entry - sl) <= p.fbMaxRisk * S) {
            // market entry at the close of the return bar: modelled as a stop order at that price on the next bar
            if (this.place(lv, pc.dir, 'FALSE_BREAK', 'market', entry, sl, 1, `ложный пробой ${pc.dir > 0 ? 'поддержки' : 'сопротивления'} на ${(depth / S).toFixed(2)} стопа и закрытие обратно за уровнем`)) {
              t.state = 'REJECTION';
              return;
            }
          }
        }
      }
    }

    // ---- BREAKOUT WITH COMPRESSION: small bars pressed against the level with rising lows / falling highs ----
    if (p.breakout && this.bars.length > p.squeezeBars + 1) {
      const last = this.bars.slice(-p.squeezeBars);
      for (const dir of [1, -1] as const) {
        // long through resistance: price below, highs within [lvl − 2L, lvl + L], lows rising, small ranges
        const pressed = last.every((x) => (dir > 0 ? x.c < lvl && x.h >= lvl - 2 * L && x.h <= lvl + L : x.c > lvl && x.l <= lvl + 2 * L && x.l >= lvl - L));
        const small = last.every((x) => x.h - x.l <= p.squeezeRangeAtr * this.atrD);
        let squeeze = true;
        for (let k = 1; k < last.length; k++) if (dir > 0 ? last[k].l < last[k - 1].l : last[k].h > last[k - 1].h) squeeze = false;
        if (!(pressed && small && squeeze)) continue;
        const entry = lvl + dir * L;
        const sl = entry - dir * S;
        if (this.place(lv, dir, 'BREAKOUT', 'stop', entry, sl, 4, `поджатие ${p.squeezeBars} баров к ${dir > 0 ? 'сопротивлению' : 'поддержке'}, стоп-ордер за уровнем + люфт`)) {
          t.state = 'COMPRESSION';
          return;
        }
      }
    }
  }

  private place(lv: DailyLevel, dir: 1 | -1, model: Model, kind: 'limit' | 'stop' | 'market', price: number, sl: number, validBars: number, trigger: string): boolean {
    const p = this.p;
    if (!this.trendAllows(dir)) return false;
    if (this.exhausted(dir, price)) return false;
    const risk = Math.abs(price - sl);
    if (!(risk > 0)) return false;
    const { ok } = this.roomOk(lv, dir, price, price + dir * p.rr * risk);
    if (!ok) return false;
    // a "market" entry at the close is modelled as a limit at that price on the next bar (it fills when the next
    // bar trades at or through it); it is charged the taker fee
    this.pending.push({ levelId: lv.id, model, dir, kind: kind === 'stop' ? 'stop' : 'limit', price, sl, risk, createdBar: this.i, expiresBar: this.i + validBars, trigger: kind === 'market' ? trigger + ' (вход по закрытию)' : trigger });
    return true;
  }

  // ---------------- orders and trades ----------------

  private fillPending(b: Candle): Setup[] {
    const p = this.p;
    const out: Setup[] = [];
    const keep: Pending[] = [];
    for (const o of this.pending) {
      if (this.i <= o.createdBar) {
        keep.push(o);
        continue;
      }
      const S = o.risk;
      // cancellation: expired, or price ran away more than cancelStops × S from the order before a fill
      const ranAway = o.dir > 0 ? b.l > o.price + p.cancelStops * S : b.h < o.price - p.cancelStops * S;
      let fill = false;
      if (o.kind === 'limit') fill = o.dir > 0 ? b.l <= o.price : b.h >= o.price;
      else fill = o.dir > 0 ? b.h >= o.price : b.l <= o.price;
      const tooMany = this.open.length >= p.maxOpen;
      if (fill && !tooMany) {
        // a stop order that gaps through fills at the open
        const px = o.kind === 'stop' && (o.dir > 0 ? b.o > o.price : b.o < o.price) ? b.o : o.price;
        const s = this.openTrade(o, b, px);
        if (s) out.push(s);
        continue;
      }
      if (this.i >= o.expiresBar || ranAway || fill) continue; // expired / cancelled / missed (position limit)
      keep.push(o);
    }
    this.pending = keep;
    return out;
  }

  private openTrade(o: Pending, b: Candle, px: number): Setup | null {
    const p = this.p;
    const lv = this.activeLevels().find((l) => l.id === o.levelId) ?? this.levels.find((l) => l.id === o.levelId);
    const risk = Math.abs(px - o.sl);
    if (!(risk > 0)) return null;
    const tp = px + o.dir * p.rr * risk;
    const tr = this.tracks.get(o.levelId);
    if (tr) tr.cooldownUntil = this.i + p.cooldownBars;
    const entryFee = o.kind === 'limit' && o.model !== 'FALSE_BREAK' ? p.feeMaker : p.feeTaker;
    const next = lv ? this.roomOk(lv, o.dir, px, tp).next : null;
    const trend = this.trend();
    const q = {
      levelStrength: Math.round(0.4 * (lv?.strength ?? 0)),
      trend: trend === o.dir ? 20 : trend === 0 ? 10 : 0,
      model: o.model === 'BOUNCE' ? 15 : o.model === 'FALSE_BREAK' ? 20 : 10,
      room: next === null ? 15 : Math.round(15 * Math.min(1, Math.abs(next - px) / (p.rr * risk * 1.5))),
      freshAtr: this.exhausted(o.dir, px) ? 0 : 10,
    };
    const id = `${this.meta.symbol}:${o.levelId}:${b.t}:${o.model}`;
    const s: Setup & { _sl: number; _entryFee: number } = {
      id,
      levelId: o.levelId,
      symbol: this.meta.symbol,
      exchange: this.meta.exchange,
      t: b.t,
      confirmedAt: b.t, // the fill happens inside this bar; outcomes are measured from this bar on
      direction: o.dir > 0 ? 'LONG' : 'SHORT',
      level: lv?.price ?? NaN,
      levelStatus: lv?.status ?? '',
      levelStrength: lv?.strength ?? 0,
      setupQuality: Object.values(q).reduce((a, x) => a + x, 0),
      distanceToLevelAtr: lv ? Math.abs(px - lv.price) / this.atrD : NaN,
      approachBars: this.i - o.createdBar,
      volumeDecay: NaN,
      volumeSlope: 0,
      approach: { ...NO_FEAT },
      volatilityContraction: NaN,
      reactionVolumeRatio: NaN,
      reactionVolumeZ: NaN,
      reactionStrengthAtr: NaN,
      sweep: o.model === 'FALSE_BREAK',
      microBos: false,
      trigger: `${o.model === 'BOUNCE' ? 'Отбой (БСУ/БПУ)' : o.model === 'FALSE_BREAK' ? 'Ложный пробой' : 'Пробой с поджатием'}: ${o.trigger}`,
      entry: px,
      invalidation: o.sl,
      sl: o.sl,
      tp,
      nextLevel: next,
      rr: p.rr,
      regime: trend > 0 ? 'uptrend' : trend < 0 ? 'downtrend' : 'range',
      reasons: ['D1_LEVEL', o.model, `STOP_${Math.round(p.stopAtr * 100)}%ATR`, `RR_${p.rr}`, trend === o.dir ? 'WITH_TREND' : trend === 0 ? 'RANGE' : 'COUNTER_TREND'],
      qualityBreakdown: q,
      levelWhy: lv?.why ?? '',
      outcome: { status: 'open', r: 0, mfeR: 0, maeR: 0, barsToReaction: null, barsToInvalidation: null } as SetupOutcome,
      _sl: o.sl,
      _entryFee: entryFee,
    };
    this.setups.push(s);
    this.open.push(s);
    return s;
  }

  /** Fees and slippage in R for one round trip. */
  private costR(s: Setup, exitFee: number, exitSlip: number): number {
    const risk = Math.abs(s.entry - s.sl);
    return ((s as Setup & { _entryFee: number })._entryFee * s.entry + (exitFee + exitSlip) * s.entry) / risk;
  }

  private updateOpen(b: Candle, justFilled: Setup[]): void {
    const p = this.p;
    const still: typeof this.open = [];
    for (const s of this.open) {
      const o = s.outcome;
      const dir = s.direction === 'LONG' ? 1 : -1;
      const risk = Math.abs(s.entry - s.sl);
      const fresh = justFilled.includes(s);
      const fav = (dir > 0 ? b.h - s.entry : s.entry - b.l) / risk;
      const adv = (dir > 0 ? s.entry - b.l : b.h - s.entry) / risk;
      const barsIn = Math.round((b.t - s.t) / p.ltfMs) + 1;
      if (!fresh) o.mfeR = Math.max(o.mfeR, fav);
      o.maeR = Math.max(o.maeR, Math.max(0, adv));
      if (o.barsToReaction === null && !fresh && fav >= 1) o.barsToReaction = barsIn;
      const stop = s._sl;
      const hitSl = dir > 0 ? b.l <= stop : b.h >= stop;
      const hitTp = !fresh && (dir > 0 ? b.h >= s.tp : b.l <= s.tp);
      if (hitSl) {
        const gross = (dir > 0 ? stop - s.entry : s.entry - stop) / risk; // −1, 0 after breakeven, > 0 when trailed
        o.status = gross < -1e-9 ? 'loss' : gross > 1e-9 ? 'win' : 'timeout';
        o.r = gross - this.costR(s, p.feeTaker, p.slippage);
        o.barsToInvalidation = barsIn;
        o.closedAt = b.t;
      } else if (hitTp) {
        o.status = 'win';
        o.r = s.rr - this.costR(s, p.feeMaker, 0);
        o.closedAt = b.t;
      } else if (barsIn >= p.maxHoldBars) {
        o.status = 'timeout';
        o.r = (dir > 0 ? b.c - s.entry : s.entry - b.c) / risk - this.costR(s, p.feeTaker, p.slippage);
        o.closedAt = b.t;
      } else {
        o.r = (dir > 0 ? b.c - s.entry : s.entry - b.c) / risk;
        // stop management uses excursions up to and including this closed bar; it applies from the next bar
        if (isFinite(p.breakevenR) && o.mfeR >= p.breakevenR && (dir > 0 ? s._sl < s.entry : s._sl > s.entry)) s._sl = s.entry;
        if (isFinite(p.trailR) && o.mfeR >= p.trailR) {
          const trail = s.entry + dir * (o.mfeR - p.trailR) * risk;
          if (dir > 0 ? trail > s._sl : trail < s._sl) s._sl = trail;
        }
        still.push(s);
      }
    }
    this.open = still;
  }
}
