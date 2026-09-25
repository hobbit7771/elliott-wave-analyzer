import { CandleBuilder, candlesFromTrades, resampleCandles, candleDelta } from '../src/core/candles.js';
import { atr, cvd, ema, macd, rsi, vwap } from '../src/core/indicators.js';
import { FootprintBuilder, analyzeBar, volumeProfile, tpoProfile } from '../src/core/footprint.js';
import type { Candle, Trade } from '../src/core/types.js';

const tr = (t: number, price: number, qty: number, side: 1 | -1): Trade => ({ t, price, qty, side });

describe('Candles from trades', () => {
  it('builds time bars with OHLC, volume and aggressive buy volume', () => {
    const c = candlesFromTrades([tr(0, 10, 1, 1), tr(30_000, 12, 2, -1), tr(59_999, 11, 1, 1), tr(60_000, 9, 3, -1)], '1m');
    expect(c).toEqual([
      { t: 0, o: 10, h: 12, l: 10, c: 11, v: 4, bv: 2, n: 3 },
      { t: 60_000, o: 9, h: 9, l: 9, c: 9, v: 3, bv: 0, n: 1 },
    ]);
    expect(candleDelta(c[0])).toBe(0);
  });
  it('builds tick bars with strictly increasing times', () => {
    const trades = Array.from({ length: 7 }, (_, i) => tr(5, 10 + i, 1, 1));
    const c = candlesFromTrades(trades, 'tick', 3);
    expect(c.map((x) => x.n)).toEqual([3, 3, 1]);
    expect(c[1].t).toBeGreaterThan(c[0].t);
  });
  it('1s bars and resampling', () => {
    const b = new CandleBuilder('1s');
    b.add(tr(100, 1, 1, 1));
    b.add(tr(1100, 2, 1, 1));
    expect(b.candles.map((c) => c.t)).toEqual([0, 1000]);
    expect(resampleCandles(b.candles, '1m')).toHaveLength(1);
  });
});

describe('Indicators', () => {
  const c: Candle[] = Array.from({ length: 60 }, (_, i) => ({ t: i * 60_000, o: 100 + i, h: 101 + i, l: 99 + i, c: 100.5 + i, v: 10, bv: 6 }));
  it('EMA seeds with SMA', () => {
    const e = ema([1, 2, 3, 4, 5], 3);
    expect(e.slice(0, 2).every(isNaN)).toBe(true);
    expect(e[2]).toBe(2);
    expect(e[3]).toBe(3);
  });
  it('RSI of a steadily rising series is 100', () => {
    expect(rsi(c, 14)[30]).toBe(100);
  });
  it('ATR of constant-range bars equals the true range', () => {
    expect(atr(c, 14)[40]).toBeCloseTo(2, 10);
  });
  it('MACD histogram = macd - signal', () => {
    const m = macd(c);
    expect(m.hist[50]).toBeCloseTo(m.macd[50] - m.signal[50], 12);
  });
  it('VWAP resets at the UTC day and CVD accumulates candle delta', () => {
    const v = vwap(c);
    expect(v[0]).toBeCloseTo((101 + 99 + 100.5) / 3, 10);
    expect(cvd(c)[9]).toBe(20);
  });
});

describe('Footprint', () => {
  it('aggregates bid x ask per row and finds POC, diagonal & stacked imbalances, unfinished auctions', () => {
    const f = new FootprintBuilder('1m', 1);
    const trades = [
      tr(1, 100, 1, -1), tr(2, 100, 1, 1),
      tr(3, 101, 9, 1), tr(4, 100, 2, -1),
      tr(5, 102, 9, 1), tr(6, 101, 1, -1),
      tr(7, 103, 10, 1), tr(8, 102, 1, -1),
      tr(9, 103, 1, -1),
    ];
    for (const t of trades) f.add(t);
    const bar = f.bars[0];
    expect(bar.rows.get(101)).toEqual({ buy: 9, sell: 1 });
    const a = analyzeBar(bar, 1, 3, 0, 3);
    expect(a.delta).toBe(9 + 9 + 10 + 1 - 1 - 2 - 1 - 1 - 1);
    expect(a.poc).toBe(103);
    // buy imbalance at 101 (9 >= 3*sell@100=3), 102 (9 >= 3*1), 103 (9 >= 3*1)
    expect(a.imbalances.get(101)).toBe(1);
    expect(a.imbalances.get(102)).toBe(1);
    expect(a.imbalances.get(103)).toBe(1);
    expect(a.stacked).toEqual([[101, 103, 1]]);
    expect(a.unfinishedHigh).toBe(true); // both sides traded at the high
    expect(a.unfinishedLow).toBe(true);
  });

  it('volume profile: POC, 70% value area, HVN/LVN', () => {
    const trades: Trade[] = [];
    const vols = [5, 20, 60, 20, 1, 1, 30, 10];
    vols.forEach((v, i) => trades.push(tr(i, 100 + i, v, 1)));
    const p = volumeProfile(trades, 1);
    expect(p.poc).toBe(102);
    expect(p.total).toBe(147);
    expect(p.val).toBeLessThanOrEqual(101);
    expect(p.vah).toBeGreaterThanOrEqual(103);
    expect(p.lvn).toContain(104);
    expect(p.hvn).toContain(102);
  });

  it('TPO letters from 30m periods', () => {
    const periods: Candle[] = [
      { t: 0, o: 0, h: 102, l: 100, c: 0, v: 0, bv: 0 },
      { t: 1, o: 0, h: 103, l: 101, c: 0, v: 0, bv: 0 },
      { t: 2, o: 0, h: 101, l: 101, c: 0, v: 0, bv: 0 },
    ];
    const p = tpoProfile(periods, 1);
    expect(p.rows.find((r) => r.price === 101)!.letters).toBe('ABC');
    expect(p.rows.find((r) => r.price === 103)!.letters).toBe('B');
    expect(p.poc).toBe(101);
    expect(p.ibHigh).toBe(103);
    expect(p.ibLow).toBe(100);
  });
});
