// The engine the site runs for every coin: Gerchik's level entries (with Raschke's ADX trend-strength
// condition) and Raschke's Holy Grail (D1), fed the same closed 15m bars. History, replay and live all use it.
import type { Candle } from '../types.js';
import type { DailyLevel } from './dailyLevels.js';
import type { LevelState, Setup } from './setupEngine.js';
import { GerchikEngine, SITE_GERCHIK_PARAMS, type GerchikParams } from './gerchik.js';
import { BOOK_RASCHKE_PARAMS, RaschkeEngine, type RaschkeParams } from './raschke.js';

export interface SiteParams {
  gerchik: GerchikParams;
  raschke: RaschkeParams;
}

export const SITE_PARAMS: SiteParams = {
  gerchik: SITE_GERCHIK_PARAMS,
  // Holy Grail with the book's entry rules (D1, ADX(14) > 30, first pullback to the 20 EMA, target = prior swing
  // extreme); exit horizon chosen on TRAIN: up to 10 days, no daily trailing
  raschke: { ...BOOK_RASCHKE_PARAMS, setups: ['HOLY_GRAIL'], hgTf: 'D1', hgAdx: 30, holdDays: 10, trailPrevDay: false },
};

export const SITE_CHOICE =
  'Две модели с фиксированными параметрами для всех монет (выбраны один раз на 14 монетах Bybit по первым 6 месяцам года, проверены на следующих 3): 1) уровни D1 по Герчику — сила ≥ 70, отбой БСУ/БПУ и пробой с поджатием, стоп 0,5 ATR(D1), цель 3:1, только по тренду D1 и при ADX(14) ≥ 20 (условие силы тренда Рашке); 2) Holy Grail Рашке (D1: ADX > 30, первый откат к EMA20, стоп-ордер выше бара отката, цель — прежний экстремум, до 10 дней). Комиссии Bybit включены в R.';

export class SiteEngine {
  readonly g: GerchikEngine;
  readonly r: RaschkeEngine;
  constructor(meta: { symbol: string; exchange: string; tick: number }, dailyBefore: readonly Candle[], readonly p: SiteParams = SITE_PARAMS) {
    this.g = new GerchikEngine(meta, dailyBefore, p.gerchik);
    this.r = new RaschkeEngine(meta, dailyBefore, p.raschke);
  }
  get levels(): DailyLevel[] {
    return this.g.levels;
  }
  get setups(): Setup[] {
    return [...this.g.setups, ...this.r.setups].sort((a, b) => a.t - b.t);
  }
  get atrDaily(): number {
    return this.g.atrDaily;
  }
  activeLevels(): DailyLevel[] {
    return this.g.activeLevels();
  }
  stateOf(levelId: string): LevelState {
    return this.g.stateOf(levelId);
  }
  step(b: Candle): Setup[] {
    return [...this.g.step(b), ...this.r.step(b)];
  }
}
