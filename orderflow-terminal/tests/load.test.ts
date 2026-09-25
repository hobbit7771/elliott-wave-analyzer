import { harness } from './fixtures/sim.js';

describe('Load: trade + depth throughput through the full engine', () => {
  it('processes >= 50k messages/s with bounded memory', () => {
    const h = harness();
    h.idle(25_000); // warm detectors
    const N = 100_000;
    const heap0 = process.memoryUsage().heapUsed;
    const t0 = performance.now();
    let msgs = 0;
    for (let i = 0; i < N; i++) {
      const side = i % 3 === 0 ? 1 : -1;
      const px = side === 1 ? 100.1 : 100.0;
      h.engine.onTrade(h.sim.trade(px, 0.01 + (i % 7) * 0.01, side as 1 | -1, false));
      msgs++;
      if (i % 10 === 0) {
        // depth churn on a few levels
        const k = i % 40;
        h.sim.set(k % 2 ? 'ask' : 'bid', +(k % 2 ? 100.1 + k * 0.1 : 100 - k * 0.1).toFixed(1), 1 + (i % 5));
        h.sim.advance(10);
        h.engine.onDiff(h.sim.diff());
        msgs++;
      }
      if (i % 25 === 0) h.engine.onTimer(h.sim.t);
    }
    const ms = performance.now() - t0;
    const rate = msgs / (ms / 1000);
    const heapMB = (process.memoryUsage().heapUsed - heap0) / 1048576;
    console.log(`load: ${msgs} msgs in ${ms.toFixed(0)} ms = ${Math.round(rate)} msg/s, heap delta ${heapMB.toFixed(1)} MB`);
    expect(rate).toBeGreaterThan(50_000);
    expect(heapMB).toBeLessThan(150);
    expect(h.engine.book.size).toBeLessThan(1000);
  });
});
