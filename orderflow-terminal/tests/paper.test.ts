import { PaperBook, backtestEvents } from '../src/core/paper.js';
import type { MarketEvent, Trade } from '../src/core/types.js';

const costs = { takerFee: 0.0005, slippageTicks: 1, tick: 0.1, assumedSpreadTicks: 1 };

describe('Paper trading', () => {
  it('fills market orders at the touch plus slippage and charges fees on both sides', () => {
    const b = new PaperBook(costs);
    const p = b.enter(1, 2, 99.9, 100, 0);
    expect(p.entry).toBeCloseTo(100.1, 10);
    const c = b.exit(p.id, 101, 101.1, 10)!;
    expect(c.exit).toBeCloseTo(100.9, 10);
    expect(c.gross).toBeCloseTo(1.6, 10);
    expect(c.fees).toBeCloseTo(100.1 * 2 * 0.0005 + 100.9 * 2 * 0.0005, 10);
    expect(c.net).toBeCloseTo(c.gross - c.fees, 10);
  });
  it('stops fill with slippage, targets at the limit; funding reduces longs when positive', () => {
    const b = new PaperBook(costs);
    b.enter(1, 1, 100, 100, 0, 99, 102);
    b.applyFunding(0.0001, 100);
    const [s] = b.mark(98.9, 99, 1);
    expect(s.reason).toBe('stop');
    expect(s.exit).toBeCloseTo(98.8, 10);
    expect(s.funding).toBeCloseTo(0.01, 10);
    b.enter(-1, 1, 100, 100, 2, 101, 98);
    const [t] = b.mark(97.9, 98, 3);
    expect(t.reason).toBe('target');
    expect(t.exit).toBe(98);
  });
  it('computes win rate, expectancy, profit factor and max drawdown', () => {
    const b = new PaperBook({ ...costs, takerFee: 0, slippageTicks: 0 });
    const seq = [2, -1, -3, 4];
    seq.forEach((d, i) => {
      const p = b.enter(1, 1, 100, 100, i * 10);
      b.exit(p.id, 100 + d, 100 + d, i * 10 + 5);
    });
    const s = b.stats();
    expect(s.trades).toBe(4);
    expect(s.winRate).toBe(0.5);
    expect(s.net).toBeCloseTo(2, 10);
    expect(s.expectancy).toBeCloseTo(0.5, 10);
    expect(s.maxDrawdown).toBeCloseTo(4, 10);
    expect(s.profitFactor).toBeCloseTo(6 / 4, 10);
  });
  it('rejects invalid orders', () => {
    expect(() => new PaperBook(costs).enter(1, 0, 1, 1, 0)).toThrow();
  });
});

describe('Event backtest', () => {
  it('enters after qualifying events and exits on target/stop/time', () => {
    const trades: Trade[] = [];
    for (let i = 0; i < 100; i++) trades.push({ t: i * 1000, price: 100 + (i < 50 ? 0 : (i - 50) * 0.1), qty: 1, side: 1 });
    const ev = (t: number, conf: number): MarketEvent => ({ id: 'e' + t, t, kind: 'iceberg', title: '', side: 'bid', price: 100, confidence: conf, explain: '', source: 'binance-futures', symbol: 'X' });
    const r = backtestEvents(trades, [ev(45_000, 80), ev(46_000, 20)], { kinds: ['iceberg'], minConfidence: 50, mode: 'follow', stopTicks: 10, targetTicks: 20, maxHoldMs: 3600_000, qty: 1 }, costs);
    expect(r.closed).toHaveLength(1);
    expect(r.closed[0].reason).toBe('target');
    expect(r.closed[0].side).toBe(1);
    expect(r.stats.trades).toBe(1);
  });
});
