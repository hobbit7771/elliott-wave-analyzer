// The engine the site runs for every coin, fed the same closed 15m bars (history, replay and live):
//  1) TREND — Donchian channel breakout (20-day high, stop 3 ATR, exit on the 10-day low, longs only, not
//     against BTC's D1 trend, not when the coin's funding shows crowded longs): the only model that held up on 5 years of daily data (see README);
//  2) LEVELS — Gerchik's entries at strong D1 levels with Raschke's trend-strength condition (ADX ≥ 20).
import type { Candle } from '../types.js';
import type { DailyLevel } from './dailyLevels.js';
import type { LevelState, OpenPosition, Setup, WorkingOrder } from './setupEngine.js';
import { GerchikEngine, SITE_GERCHIK_PARAMS, type GerchikParams } from './gerchik.js';
import { SITE_TREND_PARAMS, TrendEngine, type Funding, type TrendParams } from './trend.js';

export interface SiteParams {
  gerchik: GerchikParams;
  trend: TrendParams;
}

export const SITE_PARAMS: SiteParams = { gerchik: SITE_GERCHIK_PARAMS, trend: SITE_TREND_PARAMS };

export const SITE_CHOICE =
  'Две модели с фиксированными параметрами для всех монет. 1) Тренд: пробой 20-дневного максимума, стоп 3 ATR(20), выход по 10-дневному минимуму, только лонг, не против тренда BTC и не при перегреве лонгов (средний фандинг за 7 дней > 0,05 %/день) — выбрана скользящим walk-forward на 5 годах дневных данных 17 монет (устойчива во всех подпериодах). 2) Уровни D1 по Герчику (сила ≥ 70, отбой БСУ/БПУ и пробой с поджатием, стоп 0,5 ATR, цель 3:1) только по тренду D1 и при ADX(14) ≥ 20 (условие Рашке). Комиссии Bybit и оценка фандинга включены в R.';

export class SiteEngine {
  readonly g: GerchikEngine;
  readonly tr: TrendEngine;
  constructor(meta: { symbol: string; exchange: string; tick: number }, dailyBefore: readonly Candle[], readonly p: SiteParams = SITE_PARAMS, market: readonly Candle[] = [], funding: readonly Funding[] = []) {
    this.g = new GerchikEngine(meta, dailyBefore, p.gerchik);
    this.tr = new TrendEngine(meta, dailyBefore, p.trend, market, funding);
  }
  /** the coin's perpetual funding settlements (trend filter) */
  setFunding(f: readonly Funding[]): void {
    this.tr.setFunding(f);
  }
  /** market-factor daily candles (BTC), refreshed live when a new day has closed */
  setMarket(d: readonly Candle[]): void {
    this.tr.setMarket(d);
  }
  get levels(): DailyLevel[] {
    return this.g.levels;
  }
  get setups(): Setup[] {
    return [...this.g.setups, ...this.tr.setups].sort((a, b) => a.t - b.t);
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
  workingOrders(): WorkingOrder[] {
    return [...this.tr.workingOrders(), ...this.g.workingOrders()];
  }
  openPositions(): OpenPosition[] {
    return [...this.tr.openPositions(), ...this.g.openPositions()];
  }
  step(b: Candle): Setup[] {
    return [...this.g.step(b), ...this.tr.step(b)];
  }
}
