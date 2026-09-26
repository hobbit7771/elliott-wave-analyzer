// One engine for HISTORICAL backtests, REPLAY and LIVE: it consumes CLOSED lower-timeframe bars one by one
// (default 15m), builds H1 and D1 from them, recomputes strong D1 levels only when a D1 candle has closed,
// and runs a per-level state machine:
//
//   WATCHING → APPROACHING → COMPRESSION → TOUCH → REJECTION → CONFIRMED (✅ entry)
//                                   ↘ INVALIDATED / EXPIRED (then cooldown → WATCHING)
//
// Nothing here reads a bar that has not been passed to step() yet: no look-ahead. Outcomes (MFE/MAE/R) of a
// confirmed setup are measured by the same engine as later bars arrive.
import type { Candle } from '../types.js';
import { analyzeDailyLevels, DEFAULT_DAILY_LEVEL_PARAMS, type DailyLevel, type DailyLevelParams } from './dailyLevels.js';
import { median, relSlope, relativeVolume } from '../volumeStats.js';

export type LevelState = 'WATCHING' | 'APPROACHING' | 'COMPRESSION' | 'TOUCH' | 'REJECTION' | 'CONFIRMED' | 'INVALIDATED' | 'EXPIRED';

export interface SetupParams {
  ltfMs: number; // lower timeframe of the input bars
  approachAtr: number; // distance to level (in ATR_D1) that starts APPROACHING
  zoneAtr: number; // zone half-width around the level (ATR_D1)
  recentH1: number; // H1 bars in the "recent" window
  baselineH1: number; // H1 bars in the baseline window before it
  volDecayMax: number; // recent/baseline median H1 volume must be below this
  contractionMax: number; // recent/baseline median H1 true range must be below this
  impulseMaxAtr: number; // last 3 H1 bars' move toward the level must be below this (ATR_D1)
  reactVolMin: number; // reaction bar volume / approach median volume
  reactZMin: number;
  reactMode: 'or' | 'and';
  displacementAtr: number; // reaction body in ATR of the lower timeframe
  bosLookback: number; // micro BOS: close beyond the extreme of the last N bars before the reaction
  maxBarsAfterTouch: number;
  maxEntryAtr: number; // entry must be within this distance of the level (ATR_D1): no signals mid-range
  minStrength: number;
  minRR: number;
  requireCompression: boolean;
  cooldownBars: number;
  maxHoldBars: number; // outcome measurement horizon
  dailyLookback: number; // D1 candles used for levels
}

export const DEFAULT_SETUP_PARAMS: SetupParams = {
  ltfMs: 15 * 60_000,
  approachAtr: 1.0,
  zoneAtr: 0.15,
  recentH1: 6,
  baselineH1: 24,
  volDecayMax: 0.7,
  contractionMax: 0.85,
  impulseMaxAtr: 0.6,
  reactVolMin: 1.8,
  reactZMin: 2,
  reactMode: 'or',
  displacementAtr: 0.8,
  bosLookback: 4,
  maxBarsAfterTouch: 12,
  maxEntryAtr: 0.6,
  minStrength: 55,
  minRR: 1.0,
  requireCompression: true,
  cooldownBars: 16,
  maxHoldBars: 480,
  dailyLookback: 365,
};

export interface SetupOutcome {
  status: 'open' | 'win' | 'loss' | 'timeout';
  r: number;
  mfeR: number;
  maeR: number;
  barsToReaction: number | null; // bars until +1R
  barsToInvalidation: number | null;
  closedAt?: number;
}

export interface Setup {
  id: string;
  levelId: string;
  symbol: string;
  exchange: string;
  t: number; // open time of the trigger bar (the marker is drawn on it)
  confirmedAt: number; // close time of the trigger bar
  direction: 'LONG' | 'SHORT';
  level: number;
  levelStatus: string;
  levelStrength: number;
  setupQuality: number;
  distanceToLevelAtr: number;
  approachBars: number;
  volumeDecay: number; // recent/baseline − 1 (e.g. −0.41)
  volumeSlope: number;
  approach: Feat;
  volatilityContraction: number; // recent/baseline TR − 1
  reactionVolumeRatio: number;
  reactionVolumeZ: number;
  reactionStrengthAtr: number;
  sweep: boolean;
  microBos: boolean;
  trigger: string;
  entry: number;
  invalidation: number;
  sl: number;
  tp: number;
  nextLevel: number | null;
  rr: number;
  regime: string;
  reasons: string[];
  qualityBreakdown: Record<string, number>;
  levelWhy: string;
  outcome: SetupOutcome;
  segment?: 'TRAIN' | 'VALIDATION' | 'OOS';
}

interface LevelTrack {
  level: DailyLevel;
  state: LevelState;
  dir: 1 | -1; // +1 long from support, −1 short from resistance
  since: number; // bar index of the state change
  approachStart: number;
  compressed: boolean;
  touchBar: number;
  extreme: number; // sweep extreme since the touch
  sweep: boolean;
  approachVolumes: number[];
  feat: Feat;
  reaction?: { bar: number; ratio: number; z: number };
  cooldownUntil: number;
  lastSetupId?: string;
}

/** Approach features (all from CLOSED H1 bars built from the input bars). */
export interface Feat {
  dist: number; // distance to level / ATR_D1
  volDecay: number; // median recent H1 volume / median baseline
  volSlope: number; // regression slope of recent H1 volume (fraction of mean per bar)
  contraction: number; // median recent H1 true range / median baseline
  impulse: number; // last 3 H1 move toward the level / ATR_D1
  candleRangeAtr: number; // median recent H1 range / ATR_D1
  approachVelocity: number; // ATR_D1 per H1 bar toward the level over the recent window
  pullbackDepth: number; // largest counter-move during the recent window / ATR_D1
  barsToLevel: number; // estimated H1 bars to reach the level at the current velocity
}
const NO_FEAT: Feat = { dist: NaN, volDecay: NaN, volSlope: 0, contraction: NaN, impulse: NaN, candleRangeAtr: NaN, approachVelocity: 0, pullbackDepth: NaN, barsToLevel: NaN };

const DAY = 86_400_000;
const H1 = 3_600_000;

function trueRange(b: Candle, prevC: number): number {
  return Math.max(b.h - b.l, Math.abs(b.h - prevC), Math.abs(b.l - prevC));
}

export class LevelSetupEngine {
  readonly bars: Candle[] = [];
  readonly h1: Candle[] = [];
  readonly daily: Candle[];
  levels: DailyLevel[] = [];
  readonly tracks = new Map<string, LevelTrack>();
  readonly setups: Setup[] = [];
  private open: Setup[] = [];
  private curDay: Candle | null = null;
  private curDayComplete = false;
  private curH1: Candle | null = null;
  private atrLtf = NaN;
  private atrD = NaN;
  private i = -1;

  constructor(
    readonly meta: { symbol: string; exchange: string; tick: number },
    dailyBefore: readonly Candle[],
    readonly p: SetupParams = DEFAULT_SETUP_PARAMS,
    readonly lp: DailyLevelParams = DEFAULT_DAILY_LEVEL_PARAMS,
    private readonly maxBars = 3000,
  ) {
    this.daily = [...dailyBefore];
    this.recomputeLevels();
  }

  /** Strong D1 levels usable right now. */
  activeLevels(): DailyLevel[] {
    return this.levels.filter((l) => (l.status === 'STRONG' || l.status === 'FLIPPED') && l.strength >= this.p.minStrength);
  }

  stateOf(levelId: string): LevelState {
    return this.tracks.get(levelId)?.state ?? 'WATCHING';
  }

  get atrDaily(): number {
    return this.atrD;
  }

  /** Feed one CLOSED lower-timeframe bar. Returns setups confirmed on this bar. */
  step(b: Candle): Setup[] {
    this.i++;
    this.rollDaily(b);
    this.rollH1(b);
    const prev = this.bars[this.bars.length - 1];
    const tr = prev ? trueRange(b, prev.c) : b.h - b.l;
    this.atrLtf = isFinite(this.atrLtf) ? (this.atrLtf * 13 + tr) / 14 : tr;
    this.bars.push(b);
    if (this.bars.length > this.maxBars) this.bars.shift();
    this.updateOutcomes(b);
    if (!isFinite(this.atrD) || this.bars.length < 30) return [];
    const out: Setup[] = [];
    for (const lv of this.activeLevels()) {
      const s = this.stepLevel(lv, b);
      if (s) out.push(s);
    }
    return out;
  }

  // ---------------- timeframes ----------------

  private rollDaily(b: Candle): void {
    const day = Math.floor(b.t / DAY) * DAY;
    if (this.curDay && this.curDay.t !== day) {
      // the previous UTC day is now closed
      if (this.curDayComplete) {
        this.daily.push(this.curDay);
        this.recomputeLevels();
      }
      this.curDay = null;
    }
    if (!this.curDay) {
      this.curDay = { t: day, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, bv: b.bv ?? 0 };
      this.curDayComplete = b.t === day; // only days observed from their first bar become D1 candles
      const lastD = this.daily[this.daily.length - 1];
      if (lastD && lastD.t >= day) this.curDayComplete = false;
    } else {
      const d = this.curDay;
      d.h = Math.max(d.h, b.h);
      d.l = Math.min(d.l, b.l);
      d.c = b.c;
      d.v += b.v;
    }
  }

  private rollH1(b: Candle): void {
    const hr = Math.floor(b.t / H1) * H1;
    if (this.curH1 && this.curH1.t !== hr) {
      this.h1.push(this.curH1);
      if (this.h1.length > 400) this.h1.shift();
      this.curH1 = null;
    }
    if (!this.curH1) this.curH1 = { t: hr, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, bv: 0 };
    else {
      this.curH1.h = Math.max(this.curH1.h, b.h);
      this.curH1.l = Math.min(this.curH1.l, b.l);
      this.curH1.c = b.c;
      this.curH1.v += b.v;
    }
    // an hour is closed when its last lower-timeframe bar closed
    if (b.t + this.p.ltfMs >= hr + H1) {
      this.h1.push(this.curH1);
      if (this.h1.length > 400) this.h1.shift();
      this.curH1 = null;
    }
  }

  private recomputeLevels(): void {
    const d = this.daily.slice(-this.p.dailyLookback);
    const fresh = analyzeDailyLevels(d, this.lp, this.meta.tick);
    // keep identities (and states) of levels that are still there
    for (const l of fresh) {
      const old = this.levels.find((o) => Math.abs(o.price - l.price) <= Math.max(o.zone, l.zone) * 2);
      if (old) {
        l.id = old.id;
        const tr = this.tracks.get(old.id);
        if (tr) tr.level = l;
      }
    }
    this.levels = fresh;
    // ATR_D1 from closed daily candles only
    let a = NaN;
    for (let k = 1; k < d.length; k++) {
      const trv = trueRange(d[k], d[k - 1].c);
      a = isFinite(a) ? (a * 13 + trv) / 14 : trv;
    }
    this.atrD = a;
  }

  // ---------------- per-level state machine ----------------

  private features(lv: DailyLevel, dir: 1 | -1): Feat {
    const h = this.h1;
    const R = this.p.recentH1;
    const B = this.p.baselineH1;
    const last = this.bars[this.bars.length - 1];
    const dist = Math.abs(last.c - lv.price) / this.atrD;
    if (h.length < R + B + 1) return { ...NO_FEAT, dist };
    const recent = h.slice(-R);
    const base = h.slice(-(R + B), -R);
    const tr = (arr: Candle[], from: number) => arr.map((x, j) => trueRange(x, j ? arr[j - 1].c : h[from - 1]?.c ?? x.o));
    const volDecay = median(recent.map((x) => x.v)) / Math.max(1e-12, median(base.map((x) => x.v)));
    const contraction = median(tr(recent, h.length - R)) / Math.max(1e-12, median(tr(base, h.length - R - B)));
    const volSlope = relSlope(h.slice(-(R + 2)).map((x) => x.v));
    const c3 = h[h.length - 4]?.c ?? recent[0].o;
    const toward = dir > 0 ? c3 - last.c : last.c - c3; // move toward the level
    const first = recent[0].o;
    const velocity = ((dir > 0 ? first - last.c : last.c - first) / this.atrD) / R;
    // largest move against the approach direction inside the recent window
    let ext = dir > 0 ? Infinity : -Infinity;
    let pull = 0;
    for (const x of recent) {
      ext = dir > 0 ? Math.min(ext, x.l) : Math.max(ext, x.h);
      pull = Math.max(pull, dir > 0 ? x.h - ext : ext - x.l);
    }
    const distPx = Math.abs(last.c - lv.price) / this.atrD;
    return {
      volDecay,
      volSlope,
      contraction,
      impulse: Math.max(0, toward) / this.atrD,
      dist,
      candleRangeAtr: median(recent.map((x) => x.h - x.l)) / this.atrD,
      approachVelocity: velocity,
      pullbackDepth: pull / this.atrD,
      barsToLevel: velocity > 0 ? distPx / velocity : Infinity,
    };
  }

  private stepLevel(lv: DailyLevel, b: Candle): Setup | null {
    const p = this.p;
    let t = this.tracks.get(lv.id);
    if (!t) {
      t = { level: lv, state: 'WATCHING', dir: 1, since: this.i, approachStart: -1, compressed: false, touchBar: -1, extreme: NaN, sweep: false, approachVolumes: [], feat: { ...NO_FEAT }, cooldownUntil: -1 };
      this.tracks.set(lv.id, t);
    }
    t.level = lv;
    const zone = p.zoneAtr * this.atrD;
    const top = lv.price + zone;
    const bot = lv.price - zone;
    const set = (s: LevelState) => {
      t!.state = s;
      t!.since = this.i;
    };
    if (this.i < t.cooldownUntil) return null;
    if (t.state === 'CONFIRMED' || t.state === 'INVALIDATED' || t.state === 'EXPIRED') {
      set('WATCHING');
      t.compressed = false;
      t.reaction = undefined;
    }
    const distNow = Math.abs(b.c - lv.price) / this.atrD;

    if (t.state === 'WATCHING') {
      if (distNow <= p.approachAtr && Math.abs(b.c - lv.price) > zone) {
        t.dir = b.c > lv.price ? 1 : -1; // above a level → approaching support (long); below → resistance (short)
        t.approachStart = this.i;
        t.approachVolumes = [];
        set('APPROACHING');
      } else return null;
    }
    if (t.state === 'APPROACHING' || t.state === 'COMPRESSION') {
      t.approachVolumes.push(b.v);
      if (t.approachVolumes.length > 96) t.approachVolumes.shift();
      if (distNow > p.approachAtr * 1.3) {
        set('WATCHING');
        return null;
      }
      t.feat = this.features(lv, t.dir);
      const f = t.feat;
      const compressing = f.volDecay < p.volDecayMax && f.volSlope < 0 && f.contraction < p.contractionMax && f.impulse < p.impulseMaxAtr;
      if (t.state === 'APPROACHING' && compressing) {
        t.compressed = true;
        set('COMPRESSION');
      }
      const touched = t.dir > 0 ? b.l <= top : b.h >= bot;
      if (touched) {
        t.touchBar = this.i;
        t.extreme = t.dir > 0 ? b.l : b.h;
        t.sweep = t.dir > 0 ? b.l < bot : b.h > top;
        set('TOUCH');
      } else return null;
    }
    if (t.state === 'TOUCH' || t.state === 'REJECTION') {
      if (t.dir > 0) t.extreme = Math.min(t.extreme, b.l);
      else t.extreme = Math.max(t.extreme, b.h);
      if (t.dir > 0 ? b.l < bot : b.h > top) t.sweep = true;
      const bars = this.i - t.touchBar;
      // acceptance on the wrong side: two closes beyond the zone → invalidated
      const prev = this.bars[this.bars.length - 2];
      const beyond = (x: Candle) => (t!.dir > 0 ? x.c < bot : x.c > top);
      if (beyond(b) && prev && beyond(prev) && this.i - t.touchBar >= 1) {
        set('INVALIDATED');
        t.cooldownUntil = this.i + p.cooldownBars;
        return null;
      }
      if (bars > p.maxBarsAfterTouch) {
        set('EXPIRED');
        t.cooldownUntil = this.i + p.cooldownBars;
        return null;
      }
      // REJECTION: back beyond the zone in the reaction direction with a volume expansion vs the approach
      const approachMed = t.approachVolumes.length >= 8 ? t.approachVolumes : this.bars.slice(-17, -1).map((x) => x.v);
      const rv = relativeVolume(b.v, approachMed);
      const back = t.dir > 0 ? b.c > top : b.c < bot;
      const volOk = p.reactMode === 'and' ? rv.ratio >= p.reactVolMin && rv.z >= p.reactZMin : rv.ratio >= p.reactVolMin || rv.z >= p.reactZMin;
      if (t.state === 'TOUCH' && back && volOk) {
        t.reaction = { bar: this.i, ratio: rv.ratio, z: rv.z };
        set('REJECTION');
      }
      if (t.state === 'REJECTION') {
        const body = t.dir > 0 ? b.c - b.o : b.o - b.c;
        const prior = this.bars.slice(-(p.bosLookback + 1), -1);
        const bos = prior.length > 0 && (t.dir > 0 ? b.c > Math.max(...prior.map((x) => x.h)) : b.c < Math.min(...prior.map((x) => x.l)));
        const displaced = body >= p.displacementAtr * this.atrLtf;
        if (back && bos && displaced) return this.confirm(t, lv, b, bos);
      }
    }
    return null;
  }

  private confirm(t: LevelTrack, lv: DailyLevel, b: Candle, bos: boolean): Setup | null {
    const p = this.p;
    const entry = b.c;
    const reasons: string[] = ['D1_LEVEL', 'APPROACH'];
    const distEntry = Math.abs(entry - lv.price) / this.atrD;
    t.cooldownUntil = this.i + p.cooldownBars;
    if (distEntry > p.maxEntryAtr) {
      t.state = 'EXPIRED'; // NO SETUP: price already too far from the level (mid-range)
      return null;
    }
    if (p.requireCompression && !t.compressed) {
      t.state = 'EXPIRED'; // the sequence lacks volume decay / contraction
      return null;
    }
    const buf = 0.1 * this.atrLtf;
    const sl = t.dir > 0 ? t.extreme - buf : t.extreme + buf;
    const risk = Math.abs(entry - sl);
    if (!(risk > 0)) {
      t.state = 'EXPIRED';
      return null;
    }
    // next strong level in the trade direction
    const beyond = this.activeLevels()
      .filter((l) => l.id !== lv.id && (t.dir > 0 ? l.price > entry + 0.25 * this.atrD : l.price < entry - 0.25 * this.atrD))
      .sort((x, y) => (t.dir > 0 ? x.price - y.price : y.price - x.price));
    const next = beyond[0]?.price ?? null;
    const tp = next ?? (t.dir > 0 ? entry + 2 * risk : entry - 2 * risk);
    const rr = Math.abs(tp - entry) / risk;
    if (rr < p.minRR) {
      t.state = 'EXPIRED';
      return null;
    }
    const f = t.feat;
    if (t.compressed) reasons.push('VOL_DECAY', 'CONTRACTION');
    reasons.push(t.sweep ? 'SWEEP_RECLAIM' : 'TOUCH', 'REACTION_VOLUME', 'DISPLACEMENT');
    if (bos) reasons.push('MICRO_BOS');
    reasons.push(next !== null ? 'NEXT_LEVEL_TP' : 'TP_2R_NO_NEXT_LEVEL');
    const clamp = (x: number) => Math.max(0, Math.min(1, x));
    const body = Math.abs(b.c - b.o) / this.atrLtf;
    const q = {
      volumeDecay: 15 * clamp((1 - f.volDecay) / 0.5),
      contraction: 15 * clamp((1 - f.contraction) / 0.5),
      calmApproach: 10 * clamp(1 - f.impulse / p.impulseMaxAtr),
      reactionVolume: 20 * clamp(((t.reaction?.ratio ?? 0) - 1) / 2.5),
      displacement: 15 * clamp(body / 2),
      sweepReclaim: t.sweep ? 10 : 0,
      microBos: bos ? 5 : 0,
      rr: 10 * clamp((rr - 1) / 2),
    };
    const quality = Math.round(Object.values(q).reduce((s, x) => s + x, 0));
    const id = `${this.meta.symbol}:${lv.id}:${this.bars[this.bars.length - 1 - (this.i - t.touchBar)]?.t ?? b.t}`;
    if (t.lastSetupId === id) return null; // one setup per touch (deduplication)
    t.lastSetupId = id;
    t.state = 'CONFIRMED';
    const approachBars = t.touchBar - t.approachStart;
    const s: Setup = {
      id,
      levelId: lv.id,
      symbol: this.meta.symbol,
      exchange: this.meta.exchange,
      t: b.t,
      confirmedAt: b.t + p.ltfMs,
      direction: t.dir > 0 ? 'LONG' : 'SHORT',
      level: lv.price,
      levelStatus: lv.status,
      levelStrength: lv.strength,
      setupQuality: quality,
      distanceToLevelAtr: distEntry,
      approachBars,
      volumeDecay: f.volDecay - 1,
      volumeSlope: f.volSlope,
      approach: { ...f },
      volatilityContraction: f.contraction - 1,
      reactionVolumeRatio: t.reaction?.ratio ?? NaN,
      reactionVolumeZ: t.reaction?.z ?? NaN,
      reactionStrengthAtr: Math.abs(entry - t.extreme) / this.atrD,
      sweep: t.sweep,
      microBos: bos,
      trigger: `${t.sweep ? 'ложный пробой и возврат' : 'касание'} зоны + закрытие ${t.dir > 0 ? 'выше' : 'ниже'} зоны на ${Math.round(p.ltfMs / 60_000)}m + импульс ${body.toFixed(1)} ATR + micro BOS`,
      entry,
      invalidation: t.extreme,
      sl,
      tp,
      nextLevel: next,
      rr,
      regime: this.regime(),
      reasons,
      qualityBreakdown: Object.fromEntries(Object.entries(q).map(([k, v]) => [k, Math.round(v)])),
      levelWhy: lv.why,
      outcome: { status: 'open', r: 0, mfeR: 0, maeR: 0, barsToReaction: null, barsToInvalidation: null },
    };
    this.setups.push(s);
    this.open.push(s);
    return s;
  }

  /** Market regime at the time of the setup, from CLOSED daily candles only. */
  private regime(): string {
    const d = this.daily;
    if (d.length < 60) return 'unknown';
    const sma = (k: number, n: number) => d.slice(k - n, k).reduce((s, x) => s + x.c, 0) / n;
    const now = sma(d.length, 50);
    const before = sma(d.length - 10, 50);
    const c = d[d.length - 1].c;
    const slope = (now - before) / before;
    if (c > now && slope > 0.01) return 'uptrend';
    if (c < now && slope < -0.01) return 'downtrend';
    return 'range';
  }

  // ---------------- outcome measurement (bars after the setup only) ----------------

  private updateOutcomes(b: Candle): void {
    if (!this.open.length) return;
    const still: Setup[] = [];
    for (const s of this.open) {
      if (b.t < s.confirmedAt) {
        still.push(s);
        continue;
      }
      const o = s.outcome;
      const risk = Math.abs(s.entry - s.sl);
      const dir = s.direction === 'LONG' ? 1 : -1;
      const fav = (dir > 0 ? b.h - s.entry : s.entry - b.l) / risk;
      const adv = (dir > 0 ? s.entry - b.l : b.h - s.entry) / risk;
      const barsIn = Math.round((b.t - s.confirmedAt) / this.p.ltfMs) + 1;
      o.mfeR = Math.max(o.mfeR, fav);
      o.maeR = Math.max(o.maeR, adv);
      if (o.barsToReaction === null && fav >= 1) o.barsToReaction = barsIn;
      const hitSl = dir > 0 ? b.l <= s.sl : b.h >= s.sl;
      const hitTp = dir > 0 ? b.h >= s.tp : b.l <= s.tp;
      if (hitSl) {
        // conservative: if both are inside one bar, the stop is assumed first
        o.status = 'loss';
        o.r = -1;
        o.barsToInvalidation = barsIn;
        o.closedAt = b.t;
      } else if (hitTp) {
        o.status = 'win';
        o.r = s.rr;
        o.closedAt = b.t;
      } else if (barsIn >= this.p.maxHoldBars) {
        o.status = 'timeout';
        o.r = (dir > 0 ? b.c - s.entry : s.entry - b.c) / risk;
        o.closedAt = b.t;
      } else {
        o.r = (dir > 0 ? b.c - s.entry : s.entry - b.c) / risk; // mark-to-market while open
        still.push(s);
      }
    }
    this.open = still;
  }
}
