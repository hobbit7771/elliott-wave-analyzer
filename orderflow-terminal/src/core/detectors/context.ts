import type { BookSide, BookStats, InstrumentMeta, MarketEvent } from '../types.js';
import type { OrderBook } from '../orderbook.js';
import type { DetectorConfig } from './config.js';
import { decimalsOf, fromTick } from '../precision.js';

export type EmitInput = Omit<MarketEvent, 'source' | 'symbol'>;

export interface DetectorContext {
  meta: InstrumentMeta;
  cfg: DetectorConfig;
  book: OrderBook;
  tick: number;
  /** bucket size used for cluster / vacuum zones */
  zoneStep: number;
  now: number;
  stats: BookStats | null;
  /** 14-period ATR of 1m candles (NaN until warm) */
  atr1m: number;
  /** long-run average ATR, for ATR-adjusted thresholds */
  atrAvg: number;
  /** session cumulative delta */
  cvd: number;
  /** median aggressive trade size (NaN until warm) */
  medianTradeQty: number;
  /** true when data is fresh and the book is synced; detectors must not emit otherwise */
  gateOpen: boolean;
  emit(ev: EmitInput): void;
}

export function priceOf(ctx: DetectorContext, tick: number): number {
  return fromTick(tick, ctx.tick, ctx.meta.pricePrecision ?? decimalsOf(ctx.tick));
}

export function fp(ctx: DetectorContext, p: number): string {
  return p.toFixed(Math.max(ctx.meta.pricePrecision, decimalsOf(ctx.tick)));
}

export function fq(q: number): string {
  if (q >= 1000) return q.toFixed(0);
  if (q >= 10) return q.toFixed(1);
  return q.toFixed(3);
}

export const sideName = (s: BookSide): string => (s === 'bid' ? 'bid' : 'ask');
export const opp = (s: BookSide): BookSide => (s === 'bid' ? 'ask' : 'bid');

/** Minimum distance, in price, that counts as a "real" move for this instrument right now. */
export function moveUnit(ctx: DetectorContext, atrFrac: number, minTicks: number): number {
  const a = isFinite(ctx.atr1m) && ctx.atr1m > 0 ? ctx.atr1m * atrFrac : 0;
  return Math.max(a, minTicks * ctx.tick);
}
