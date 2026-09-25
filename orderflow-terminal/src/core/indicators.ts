// Classic indicators computed from real candles. All return arrays aligned with input (NaN warm-up).
import type { Candle } from './types.js';
import { candleDelta } from './candles.js';

export function ema(values: number[], period: number): number[] {
  const out = new Array<number>(values.length).fill(NaN);
  if (period <= 0) return out;
  const k = 2 / (period + 1);
  let prev = NaN;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (i < period - 1) {
      sum += v;
      continue;
    }
    if (i === period - 1) {
      sum += v;
      prev = sum / period;
    } else prev = v * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** Wilder smoothing (RMA). */
export function rma(values: number[], period: number): number[] {
  const out = new Array<number>(values.length).fill(NaN);
  let prev = NaN;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    if (i < period) {
      sum += values[i];
      if (i === period - 1) {
        prev = sum / period;
        out[i] = prev;
      }
      continue;
    }
    prev = (prev * (period - 1) + values[i]) / period;
    out[i] = prev;
  }
  return out;
}

export function trueRange(c: Candle[]): number[] {
  return c.map((x, i) => (i === 0 ? x.h - x.l : Math.max(x.h - x.l, Math.abs(x.h - c[i - 1].c), Math.abs(x.l - c[i - 1].c))));
}

export function atr(c: Candle[], period = 14): number[] {
  return rma(trueRange(c), period);
}

export function rsi(c: Candle[], period = 14): number[] {
  const gains: number[] = [];
  const losses: number[] = [];
  for (let i = 0; i < c.length; i++) {
    const d = i === 0 ? 0 : c[i].c - c[i - 1].c;
    gains.push(Math.max(0, d));
    losses.push(Math.max(0, -d));
  }
  // first element carries no change; smooth from index 1
  const g = rma(gains.slice(1), period);
  const l = rma(losses.slice(1), period);
  const out = [NaN];
  for (let i = 0; i < g.length; i++) {
    if (isNaN(g[i])) out.push(NaN);
    else out.push(l[i] === 0 ? 100 : 100 - 100 / (1 + g[i] / l[i]));
  }
  return out;
}

export function macd(c: Candle[], fast = 12, slow = 26, signal = 9): { macd: number[]; signal: number[]; hist: number[] } {
  const close = c.map((x) => x.c);
  const f = ema(close, fast);
  const s = ema(close, slow);
  const m = f.map((v, i) => v - s[i]);
  const firstValid = m.findIndex((v) => !isNaN(v));
  const sig = new Array<number>(m.length).fill(NaN);
  if (firstValid >= 0) {
    const e = ema(m.slice(firstValid), signal);
    for (let i = 0; i < e.length; i++) sig[firstValid + i] = e[i];
  }
  return { macd: m, signal: sig, hist: m.map((v, i) => v - sig[i]) };
}

/** Session VWAP, reset at UTC day boundaries (crypto convention). Uses typical price. */
export function vwap(c: Candle[], sessionMs = 86_400_000): number[] {
  const out: number[] = [];
  let pv = 0;
  let vv = 0;
  let sess = -1;
  for (const x of c) {
    const s = Math.floor(x.t / sessionMs);
    if (s !== sess) {
      sess = s;
      pv = 0;
      vv = 0;
    }
    const tp = (x.h + x.l + x.c) / 3;
    pv += tp * x.v;
    vv += x.v;
    out.push(vv > 0 ? pv / vv : x.c);
  }
  return out;
}

/** Cumulative volume delta from per-candle taker buy volume. */
export function cvd(c: Candle[]): number[] {
  let s = 0;
  return c.map((x) => (s += candleDelta(x)));
}
