import { walkBook } from '../src/core/paperDepth.js';
import { PaperService, type BookView } from '../src/server/services/paper.js';
import { AlertService } from '../src/server/services/alerts.js';
import type { MarketEvent } from '../src/core/types.js';

const book = (gate = true): BookView => ({ t: Date.now(), gate, tick: 0.1, bids: [[99.9, 1], [99.8, 2], [99.7, 5]], asks: [[100, 1], [100.1, 2], [100.2, 5]] });

describe('Depth-aware paper fills', () => {
  it('walks the book and never fills a large order at the best price', () => {
    const w = walkBook([[100, 1], [100.1, 2]], 2);
    expect(w.ok).toBe(true);
    expect(w.avgPrice).toBeCloseTo((100 + 100.1) / 2, 10);
    expect(walkBook([[100, 1]], 5).ok).toBe(false);
  });

  it('rejects orders when data is not reliable or depth is insufficient; SL triggers server-side', async () => {
    const saved: unknown[] = [];
    const p = new PaperService(undefined, async (s) => void saved.push(s), () => {});
    p.onBook('k', book(false));
    expect(() => p.order('k', 1, 1)).toThrow(/not reliable/);
    p.onBook('k', book());
    expect(() => p.order('k', 1, 100)).toThrow(/visible depth/);
    const pos = p.order('k', 1, 2, 99.5, 101);
    expect(pos.entry).toBeCloseTo((100 * 1 + 100.1 * 1) / 2, 10);
    // outage while price crosses the stop: nothing happens during the outage
    p.onBook('k', { ...book(false), bids: [[99.0, 10]], asks: [[99.1, 10]] });
    expect(p.state.open).toHaveLength(1);
    // back online: stop executes at the book available now, flagged as delayed
    p.onBook('k', { ...book(), bids: [[99.0, 10]], asks: [[99.1, 10]] });
    expect(p.state.open).toHaveLength(0);
    const c = p.state.closed[0];
    expect(c.reason).toBe('stop');
    expect(c.delayed).toBe(true);
    expect(c.exit).toBe(99.0);
    expect(p.state.outages[0].t1).not.toBeNull();
  });

  it('charges funding once per funding rollover', () => {
    const p = new PaperService(undefined, async () => {}, () => {});
    p.onBook('k', book());
    p.order('k', 1, 1);
    p.onFunding('k', 0.0001, 100, 1000);
    expect(p.state.open[0].funding).toBe(0);
    p.onFunding('k', 0.0001, 100, 1000);
    p.onFunding('k', 0.0001, 100, 2000);
    expect(p.state.open[0].funding).toBeCloseTo(0.01, 10);
  });
});

describe('Server alerts', () => {
  const ev = (id: string, t: number, conf: number, kind: MarketEvent['kind'] = 'absorption'): MarketEvent => ({ id, t, kind, title: '', price: 1, confidence: conf, explain: '', source: 'binance-futures', symbol: 'BTCUSDT', data: { volume: 5 } });
  it('filters by score/volume, dedupes updates, applies cooldown and one-per-bar', () => {
    const fired: unknown[] = [];
    const a = new AlertService([{ id: 1, enabled: true, symbol: 'BTCUSDT', kinds: ['absorption'], minScore: 60, minVolume: 3, tf: '1m', cooldownMs: 0, sound: false, notify: true }], async () => true, (x) => fired.push(x));
    expect(a.onEvent(ev('a', 1000, 50))).toHaveLength(0);
    expect(a.onEvent(ev('a', 1000, 70))).toHaveLength(1);
    expect(a.onEvent(ev('a', 1000, 80))).toHaveLength(0); // same event updated
    expect(a.onEvent(ev('b', 30_000, 80))).toHaveLength(0); // same 1m bar
    expect(a.onEvent(ev('c', 61_000, 80))).toHaveLength(1);
    expect(fired).toHaveLength(2);
  });
});
