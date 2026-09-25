// Timeframes and trade -> candle aggregation.
import type { Candle, Trade } from './types.js';

export type Timeframe = 'tick' | '1s' | '1m' | '3m' | '5m' | '15m' | '30m' | '1h' | '4h' | '1d';

export const TIMEFRAMES: Timeframe[] = ['tick', '1s', '1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'];

export const TF_MS: Record<Exclude<Timeframe, 'tick'>, number> = {
  '1s': 1_000,
  '1m': 60_000,
  '3m': 180_000,
  '5m': 300_000,
  '15m': 900_000,
  '30m': 1_800_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
};

export const TF_LABEL: Record<Timeframe, string> = {
  tick: 'Tick',
  '1s': '1s',
  '1m': '1m',
  '3m': '3m',
  '5m': '5m',
  '15m': '15m',
  '30m': '30m',
  '1h': '1H',
  '4h': '4H',
  '1d': '1D',
};

export function isTimeframe(s: string): s is Timeframe {
  return (TIMEFRAMES as string[]).includes(s);
}

export function bucketStart(t: number, tf: Exclude<Timeframe, 'tick'>): number {
  const ms = TF_MS[tf];
  return Math.floor(t / ms) * ms;
}

/**
 * Builds candles from a trade stream. For time frames the open time is the bucket start;
 * for 'tick' every `ticksPerBar` trades form a bar (open time = first trade time).
 * Returns the (possibly new) current candle and whether a new bar was opened.
 */
export class CandleBuilder {
  candles: Candle[] = [];
  private tickCount = 0;
  constructor(public tf: Timeframe, public ticksPerBar = 100, public maxBars = 5000) {}

  load(history: Candle[]): void {
    this.candles = history.slice(-this.maxBars);
    this.tickCount = this.tf === 'tick' && this.candles.length ? (this.candles[this.candles.length - 1].n ?? 0) : 0;
  }

  add(tr: Trade): { candle: Candle; opened: boolean } {
    const last = this.candles[this.candles.length - 1];
    let opened = false;
    let c: Candle;
    if (this.tf === 'tick') {
      if (!last || this.tickCount >= this.ticksPerBar) {
        // tick bars need unique increasing times for charting
        const t = last && tr.t <= last.t ? last.t + 1 : tr.t;
        c = { t, o: tr.price, h: tr.price, l: tr.price, c: tr.price, v: 0, bv: 0, n: 0 };
        this.candles.push(c);
        this.tickCount = 0;
        opened = true;
      } else c = last;
      this.tickCount++;
    } else {
      const bt = bucketStart(tr.t, this.tf);
      if (!last || bt > last.t) {
        c = { t: bt, o: tr.price, h: tr.price, l: tr.price, c: tr.price, v: 0, bv: 0, n: 0 };
        this.candles.push(c);
        opened = true;
      } else if (bt < last.t) {
        // late trade for an older bar: fold into its bar if present
        const old = this.findBar(bt);
        if (old) applyTrade(old, tr);
        return { candle: old ?? last, opened: false };
      } else c = last;
    }
    applyTrade(c, tr);
    if (this.candles.length > this.maxBars) this.candles.splice(0, this.candles.length - this.maxBars);
    return { candle: c, opened };
  }

  private findBar(t: number): Candle | undefined {
    for (let i = this.candles.length - 1; i >= 0 && i >= this.candles.length - 50; i--) if (this.candles[i].t === t) return this.candles[i];
    return undefined;
  }
}

export function applyTrade(c: Candle, tr: Trade): void {
  if (tr.price > c.h) c.h = tr.price;
  if (tr.price < c.l) c.l = tr.price;
  c.c = tr.price;
  c.v += tr.qty;
  if (tr.side === 1) c.bv += tr.qty;
  c.n = (c.n ?? 0) + 1;
}

export function candlesFromTrades(trades: Trade[], tf: Timeframe, ticksPerBar = 100): Candle[] {
  const b = new CandleBuilder(tf, ticksPerBar, Number.MAX_SAFE_INTEGER);
  for (const t of trades) b.add(t);
  return b.candles;
}

/** Merge fine candles into a coarser time frame (both must be time-based). */
export function resampleCandles(src: Candle[], tf: Exclude<Timeframe, 'tick'>): Candle[] {
  const out: Candle[] = [];
  for (const c of src) {
    const bt = bucketStart(c.t, tf);
    const last = out[out.length - 1];
    if (!last || last.t !== bt) out.push({ ...c, t: bt });
    else {
      last.h = Math.max(last.h, c.h);
      last.l = Math.min(last.l, c.l);
      last.c = c.c;
      last.v += c.v;
      last.bv += c.bv;
      last.n = (last.n ?? 0) + (c.n ?? 0);
    }
  }
  return out;
}

/** Candle delta = aggressive buy - aggressive sell volume. */
export const candleDelta = (c: Candle): number => 2 * c.bv - c.v;
