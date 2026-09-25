// Footprint (bid x ask per price per bar), imbalances, POC, unfinished auctions, volume profile, TPO.
import type { Candle, Trade } from './types.js';
import { TF_MS, type Timeframe } from './candles.js';
import { decimalsOf } from './precision.js';

export interface FpRow {
  /** aggressive buys (executed at ask) */
  buy: number;
  /** aggressive sells (executed at bid) */
  sell: number;
}

export interface FootprintBar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  rows: Map<number, FpRow>; // key = row index (floor(price/step))
  buy: number;
  sell: number;
  trades: number;
}

export interface FootprintAnalysis {
  delta: number;
  volume: number;
  poc: number;              // row index
  /** row index -> +1 buy imbalance (ask vs diagonal bid), -1 sell imbalance */
  imbalances: Map<number, 1 | -1>;
  /** stacked imbalance zones [loRow, hiRow, dir] */
  stacked: [number, number, 1 | -1][];
  unfinishedHigh: boolean;
  unfinishedLow: boolean;
  /** max(|delta|) row */
  maxDeltaRow: number;
}

export class FootprintBuilder {
  bars: FootprintBar[] = [];
  private tickCount = 0;
  constructor(
    public tf: Timeframe,
    public step: number,
    public maxBars = 400,
    public ticksPerBar = 100,
  ) {}

  add(tr: Trade): FootprintBar {
    let bar = this.bars[this.bars.length - 1];
    if (this.tf === 'tick') {
      if (!bar || this.tickCount >= this.ticksPerBar) {
        const t = bar && tr.t <= bar.t ? bar.t + 1 : tr.t;
        bar = newBar(t, tr.price);
        this.bars.push(bar);
        this.tickCount = 0;
      }
      this.tickCount++;
    } else {
      const ms = TF_MS[this.tf];
      const bt = Math.floor(tr.t / ms) * ms;
      if (!bar || bt > bar.t) {
        bar = newBar(bt, tr.price);
        this.bars.push(bar);
      } else if (bt < bar.t) {
        const old = this.bars.find((b) => b.t === bt);
        if (!old) return bar;
        bar = old;
      }
    }
    addToBar(bar, tr, this.step);
    if (this.bars.length > this.maxBars) this.bars.splice(0, this.bars.length - this.maxBars);
    return bar;
  }
}

function newBar(t: number, p: number): FootprintBar {
  return { t, o: p, h: p, l: p, c: p, rows: new Map(), buy: 0, sell: 0, trades: 0 };
}

export function rowOf(price: number, step: number): number {
  return Math.floor(price / step + 1e-9);
}

export function addToBar(bar: FootprintBar, tr: Trade, step: number): void {
  if (tr.price > bar.h) bar.h = tr.price;
  if (tr.price < bar.l) bar.l = tr.price;
  bar.c = tr.price;
  const r = rowOf(tr.price, step);
  let row = bar.rows.get(r);
  if (!row) {
    row = { buy: 0, sell: 0 };
    bar.rows.set(r, row);
  }
  if (tr.side === 1) {
    row.buy += tr.qty;
    bar.buy += tr.qty;
  } else {
    row.sell += tr.qty;
    bar.sell += tr.qty;
  }
  bar.trades++;
}

/**
 * Diagonal imbalance (standard footprint convention):
 *   buy imbalance at row r  if buy[r]  >= ratio * sell[r-1]
 *   sell imbalance at row r if sell[r] >= ratio * buy[r+1]
 * A zero on the compared side counts as imbalance only when the volume >= minVolume.
 */
export function analyzeBar(bar: FootprintBar, step: number, ratio = 3, minVolume = 0, stackCount = 3): FootprintAnalysis {
  const imbalances = new Map<number, 1 | -1>();
  // when the diagonal cell is empty, only count volume above the bar's average side-cell volume
  const zeroFloor = Math.max(minVolume, bar.rows.size ? (bar.buy + bar.sell) / (2 * bar.rows.size) : 0, 1e-12);
  let poc = NaN;
  let pocVol = -1;
  let maxDeltaRow = NaN;
  let maxD = -1;
  for (const [r, row] of bar.rows) {
    const v = row.buy + row.sell;
    if (v > pocVol) {
      pocVol = v;
      poc = r;
    }
    const d = Math.abs(row.buy - row.sell);
    if (d > maxD) {
      maxD = d;
      maxDeltaRow = r;
    }
    const below = bar.rows.get(r - 1);
    const above = bar.rows.get(r + 1);
    const sb = below ? below.sell : 0;
    const ba = above ? above.buy : 0;
    if (row.buy >= minVolume && row.buy > 0 && row.buy >= ratio * sb && (sb > 0 || row.buy >= zeroFloor)) imbalances.set(r, 1);
    else if (row.sell >= minVolume && row.sell > 0 && row.sell >= ratio * ba && (ba > 0 || row.sell >= zeroFloor)) imbalances.set(r, -1);
  }
  const stacked: [number, number, 1 | -1][] = [];
  const keys = [...imbalances.keys()].sort((a, b) => a - b);
  let start = NaN;
  let prev = NaN;
  let dir: 1 | -1 = 1;
  const flush = () => {
    if (!isNaN(start) && prev - start + 1 >= stackCount) stacked.push([start, prev, dir]);
  };
  for (const k of keys) {
    const d = imbalances.get(k)!;
    if (!isNaN(prev) && k === prev + 1 && d === dir) {
      prev = k;
      continue;
    }
    flush();
    start = k;
    prev = k;
    dir = d;
  }
  flush();
  const hiRow = bar.rows.get(rowOf(bar.h, step));
  const loRow = bar.rows.get(rowOf(bar.l, step));
  return {
    delta: bar.buy - bar.sell,
    volume: bar.buy + bar.sell,
    poc,
    imbalances,
    stacked,
    // an auction is "unfinished" at an extreme when both buyers and sellers traded there
    unfinishedHigh: !!hiRow && hiRow.buy > 0 && hiRow.sell > 0,
    unfinishedLow: !!loRow && loRow.buy > 0 && loRow.sell > 0,
    maxDeltaRow,
  };
}

export interface ProfileRow {
  row: number;
  price: number;
  buy: number;
  sell: number;
  vol: number;
}

export interface VolumeProfile {
  step: number;
  rows: ProfileRow[];   // ascending price
  poc: number;          // price
  vah: number;
  val: number;
  total: number;
  hvn: number[];        // prices
  lvn: number[];
  from: number;
  to: number;
}

/** Exact volume profile from trades, with 70% value area and HVN/LVN detection. */
export function volumeProfile(trades: Trade[], step: number, valueArea = 0.7): VolumeProfile {
  const m = new Map<number, ProfileRow>();
  const dec = decimalsOf(step);
  let total = 0;
  for (const t of trades) {
    const r = rowOf(t.price, step);
    let row = m.get(r);
    if (!row) {
      row = { row: r, price: +(r * step).toFixed(dec), buy: 0, sell: 0, vol: 0 };
      m.set(r, row);
    }
    if (t.side === 1) row.buy += t.qty;
    else row.sell += t.qty;
    row.vol += t.qty;
    total += t.qty;
  }
  return finishProfile([...m.values()].sort((a, b) => a.row - b.row), step, total, valueArea, trades.length ? trades[0].t : 0, trades.length ? trades[trades.length - 1].t : 0);
}

export function finishProfile(rows: ProfileRow[], step: number, total: number, valueArea: number, from: number, to: number): VolumeProfile {
  if (!rows.length) return { step, rows, poc: NaN, vah: NaN, val: NaN, total: 0, hvn: [], lvn: [], from, to };
  // fill empty rows so LVN detection sees gaps
  const filled: ProfileRow[] = [];
  const dec = decimalsOf(step);
  for (let r = rows[0].row, i = 0; r <= rows[rows.length - 1].row; r++) {
    if (rows[i] && rows[i].row === r) filled.push(rows[i++]);
    else filled.push({ row: r, price: +(r * step).toFixed(dec), buy: 0, sell: 0, vol: 0 });
  }
  let pi = 0;
  for (let i = 1; i < filled.length; i++) if (filled[i].vol > filled[pi].vol) pi = i;
  let lo = pi;
  let hi = pi;
  let acc = filled[pi].vol;
  while (acc < total * valueArea && (lo > 0 || hi < filled.length - 1)) {
    const up = hi < filled.length - 1 ? filled[hi + 1].vol : -1;
    const dn = lo > 0 ? filled[lo - 1].vol : -1;
    if (up >= dn) acc += filled[++hi].vol;
    else acc += filled[--lo].vol;
  }
  // HVN/LVN on a 3-row smoothed profile
  const sm = filled.map((_, i) => {
    let s = 0;
    let n = 0;
    for (let j = i - 1; j <= i + 1; j++) if (j >= 0 && j < filled.length) (s += filled[j].vol, n++);
    return s / n;
  });
  const mean = total / filled.length;
  const hvn: number[] = [];
  const lvn: number[] = [];
  for (let i = 1; i < sm.length - 1; i++) {
    if (sm[i] >= sm[i - 1] && sm[i] >= sm[i + 1] && sm[i] >= 1.5 * mean) hvn.push(filled[i].price);
    if (sm[i] <= sm[i - 1] && sm[i] <= sm[i + 1] && sm[i] <= 0.5 * mean) lvn.push(filled[i].price);
  }
  return { step, rows: filled, poc: filled[pi].price, vah: filled[hi].price, val: filled[lo].price, total, hvn, lvn, from, to };
}

export interface TpoProfile {
  step: number;
  /** price row -> letters string */
  rows: { price: number; letters: string }[];
  poc: number;
  vah: number;
  val: number;
  ibHigh: number;
  ibLow: number;
  periods: number;
}

const TPO_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXabcdefghijklmnopqrstuvwx';

/** TPO from 30-minute candles of one session: each period marks every row between its low and high. */
export function tpoProfile(periods: Candle[], step: number): TpoProfile {
  const dec = decimalsOf(step);
  const m = new Map<number, string>();
  periods.forEach((c, i) => {
    const L = TPO_LETTERS[i % TPO_LETTERS.length];
    for (let r = rowOf(c.l, step); r <= rowOf(c.h, step); r++) m.set(r, (m.get(r) ?? '') + L);
  });
  const keys = [...m.keys()].sort((a, b) => a - b);
  const rows = keys.map((r) => ({ price: +(r * step).toFixed(dec), letters: m.get(r)! }));
  const prof = finishProfile(
    keys.map((r) => ({ row: r, price: +(r * step).toFixed(dec), buy: 0, sell: 0, vol: m.get(r)!.length })),
    step,
    keys.reduce((s, r) => s + m.get(r)!.length, 0),
    0.7,
    periods[0]?.t ?? 0,
    periods[periods.length - 1]?.t ?? 0,
  );
  const ib = periods.slice(0, 2);
  return {
    step,
    rows,
    poc: prof.poc,
    vah: prof.vah,
    val: prof.val,
    ibHigh: ib.length ? Math.max(...ib.map((c) => c.h)) : NaN,
    ibLow: ib.length ? Math.min(...ib.map((c) => c.l)) : NaN,
    periods: periods.length,
  };
}
