// TEST-ONLY synthetic exchange. Deterministic (no randomness). Never imported by production code
// (enforced by tests/no-fake-data.test.ts).
import type { BookSide, BookSnapshot, Candle, DepthDiff, InstrumentMeta, Level, Trade } from '../../src/core/types.js';
import { MarketEngine } from '../../src/core/engine.js';
import { DEFAULT_DETECTOR_CONFIG, type DetectorConfig } from '../../src/core/detectors/config.js';

export const TEST_META: InstrumentMeta = {
  source: 'binance-futures',
  symbol: 'TESTUSDT',
  base: 'TEST',
  quote: 'USDT',
  tickSize: 0.1,
  stepSize: 0.001,
  pricePrecision: 1,
  qtyPrecision: 3,
};

export const T0 = 1_700_000_000_000;

export class SimExchange {
  bids = new Map<number, number>();
  asks = new Map<number, number>();
  lastId = 1000;
  t = T0;
  tradeId = 1;
  private changed = { bid: new Map<number, number>(), ask: new Map<number, number>() };

  constructor(public tick = 0.1) {}

  k(p: number): number {
    return Math.round(p / this.tick);
  }
  p(k: number): number {
    return +(k * this.tick).toFixed(4);
  }

  /** Symmetric deterministic book around mid: `levels` per side with a repeating size pattern. */
  seed(bestBid: number, levels = 200, pattern = [3, 5, 4, 6, 2, 5, 3, 4]): void {
    const bb = this.k(bestBid);
    for (let i = 0; i < levels; i++) {
      this.set('bid', this.p(bb - i), pattern[i % pattern.length]);
      this.set('ask', this.p(bb + 1 + i), pattern[(i + 3) % pattern.length]);
    }
    this.changed = { bid: new Map(), ask: new Map() };
  }

  set(side: BookSide, price: number, qty: number): void {
    const m = side === 'bid' ? this.bids : this.asks;
    const k = this.k(price);
    if (qty <= 0) m.delete(k);
    else m.set(k, qty);
    this.changed[side].set(k, Math.max(0, qty));
  }

  qty(side: BookSide, price: number): number {
    return (side === 'bid' ? this.bids : this.asks).get(this.k(price)) ?? 0;
  }

  snapshot(): BookSnapshot {
    const bids: Level[] = [...this.bids].sort((a, b) => b[0] - a[0]).map(([k, q]) => [this.p(k), q]);
    const asks: Level[] = [...this.asks].sort((a, b) => a[0] - b[0]).map(([k, q]) => [this.p(k), q]);
    return { lastUpdateId: this.lastId, t: this.t, bids, asks };
  }

  /** Emit the accumulated changes as one futures-style diff (U, u, pu). */
  diff(): DepthDiff {
    const U = this.lastId + 1;
    const u = this.lastId + 1 + Math.max(0, this.changed.bid.size + this.changed.ask.size - 1);
    const d: DepthDiff = {
      t: this.t,
      firstId: U,
      lastId: u,
      prevLastId: this.lastId,
      bids: [...this.changed.bid].map(([k, q]) => [this.p(k), q]),
      asks: [...this.changed.ask].map(([k, q]) => [this.p(k), q]),
    };
    this.lastId = u;
    this.changed = { bid: new Map(), ask: new Map() };
    return d;
  }

  /** Aggressive trade; by default consumes the displayed passive level. */
  trade(price: number, qty: number, side: 1 | -1, deplete = true): Trade {
    if (deplete) {
      const passive: BookSide = side === -1 ? 'bid' : 'ask';
      this.set(passive, price, Math.max(0, +(this.qty(passive, price) - qty).toFixed(6)));
    }
    return { t: this.t, price, qty, side, id: this.tradeId++ };
  }

  advance(ms: number): void {
    this.t += ms;
  }
}

export function flatCandles(n: number, price: number, range: number, endT: number, vol = 100): Candle[] {
  const out: Candle[] = [];
  for (let i = n; i > 0; i--) {
    const t = Math.floor(endT / 60_000) * 60_000 - i * 60_000;
    out.push({ t, o: price, h: price + range / 2, l: price - range / 2, c: price, v: vol, bv: vol / 2, n: 50 });
  }
  return out;
}

export interface Harness {
  sim: SimExchange;
  engine: MarketEngine;
  events: import('../../src/core/types.js').MarketEvent[];
  step(trades: [number, number, 1 | -1, boolean?][], ms?: number): void;
  idle(ms: number, stepMs?: number): void;
}

/** Builds a synced engine over a seeded SimExchange. */
export function harness(cfg: DetectorConfig = DEFAULT_DETECTOR_CONFIG, bestBid = 100.0): Harness {
  const sim = new SimExchange(TEST_META.tickSize);
  sim.seed(bestBid);
  const events: import('../../src/core/types.js').MarketEvent[] = [];
  const engine = new MarketEngine(TEST_META, cfg, { syncMode: 'futures', heatStep: 0.1, heatHalfBuckets: 100, heatIntervalMs: 1000, pruneTicks: 5000 }, {
    event: (e) => {
      const i = events.findIndex((x) => x.id === e.id);
      if (i >= 0) events[i] = e;
      else events.push(e);
    },
  });
  engine.seedCandles(flatCandles(60, bestBid, 1.0, sim.t));
  engine.beginSync();
  const first = sim.diff(); // empty diff bridging the snapshot
  engine.onDiff({ ...first, firstId: sim.lastId, lastId: sim.lastId, prevLastId: sim.lastId - 1 });
  const snap = sim.snapshot();
  const r = engine.onSnapshot(snap);
  if (!r.ok) throw new Error('harness sync failed: ' + r.reason);
  engine.setGate(true);
  const h: Harness = {
    sim,
    engine,
    events,
    step(trades, ms = 100) {
      for (const [p, q, s, dep] of trades) engine.onTrade(sim.trade(p, q, s, dep ?? true));
      sim.advance(ms);
      engine.onDiff(sim.diff());
      engine.onTimer(sim.t);
    },
    idle(ms, stepMs = 250) {
      for (let x = 0; x < ms; x += stepMs) {
        sim.advance(stepMs);
        engine.onDiff(sim.diff());
        engine.onTimer(sim.t);
      }
    },
  };
  return h;
}
