import { Store } from '../src/web/store.js';

describe('Client trade buffer', () => {
  it('keeps trades that share a millisecond across batch boundaries and drops only true overlap (by id)', () => {
    const s = new Store();
    s.addTrades([{ t: 1000, price: 1, qty: 1, side: 1, id: 10 }, { t: 1000, price: 1, qty: 1, side: -1, id: 11 }]);
    s.addTrades([{ t: 1000, price: 1, qty: 2, side: 1, id: 12 }, { t: 1001, price: 1, qty: 1, side: 1, id: 13 }]);
    // re-sent recent batch on (re)subscribe
    s.addTrades([{ t: 1000, price: 1, qty: 1, side: -1, id: 11 }, { t: 1001, price: 1, qty: 1, side: 1, id: 13 }]);
    expect(s.trades.map((t) => t.id)).toEqual([10, 11, 12, 13]);
    expect(s.tradeSeq).toBe(4);
  });

  it('prepends history without duplicating the live buffer', () => {
    const s = new Store();
    s.addTrades([{ t: 1000, price: 1, qty: 1, side: 1, id: 10 }]);
    s.prependTrades([{ t: 999, price: 1, qty: 1, side: 1, id: 8 }, { t: 1000, price: 1, qty: 1, side: 1, id: 9 }, { t: 1000, price: 1, qty: 1, side: 1, id: 10 }], 0);
    expect(s.trades.map((t) => t.id)).toEqual([8, 9, 10]);
  });
});
