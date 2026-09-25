// Server performance under a continuous stream (10 depth diffs/s + trades) with a browser-like client.
import { spawn } from 'node:child_process';
import { rmSync } from 'node:fs';
import WebSocket from 'ws';
import { freePort, startTestVenue } from './testVenue.js';

it('keeps event-loop lag and memory low while ingesting, recording and fanning out', async () => {
  const port = await freePort();
  rmSync(`/tmp/oft-perf-${port}.sqlite`, { force: true });
  const v = await startTestVenue();
  const s = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', 'dist/server/index.js'], {
    env: { ...process.env, PORT: String(port), DB_PATH: `/tmp/oft-perf-${port}.sqlite`, BINANCE_FUTURES_REST: v.url, BINANCE_FUTURES_WS_BASE: v.wsUrl, DEFAULT_SYMBOLS: 'binance-futures:TESTUSDT', BACKFILL_MINUTES: '1' },
  });
  await new Promise((r) => setTimeout(r, 1500));
  // three clients subscribed to the live stream
  const counts = [0, 0, 0];
  const clients = counts.map((_, i) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws`);
    ws.on('open', () => ws.send(JSON.stringify({ op: 'sub', source: 'binance-futures', symbol: 'TESTUSDT' })));
    ws.on('message', () => counts[i]++);
    return ws;
  });
  await new Promise((r) => setTimeout(r, 30_000));
  const perf = await (await fetch(`http://127.0.0.1:${port}/api/perf`)).json();
  const t0 = performance.now();
  const hm = await (await fetch(`http://127.0.0.1:${port}/api/heatmap?source=binance-futures&symbol=TESTUSDT&from=${Date.now() - 900_000}`)).json();
  const heatMs = performance.now() - t0;
  const st = perf.hub.sessions[0].status;
  console.log(JSON.stringify({ memoryMB: perf.memoryMB, eventLoopLagMs: perf.eventLoopLagMs, db: perf.db, msgsPerClient: counts, heatmapQueryMs: Math.round(heatMs), heatCols: hm.cols.length, feed: { trades: st.trades, depthUpdates: st.depthUpdates, gaps: st.gaps, dropped: st.dropped, latencyMs: st.latencyMs } }, null, 1));
  for (const c of clients) c.close();
  const exited = new Promise((r) => s.once('exit', r));
  s.kill('SIGTERM');
  await exited;
  await v.close();
  expect(st.state).toBe('connected');
  expect(st.gaps).toBe(0);
  expect(perf.eventLoopLagMs.p99).toBeLessThan(50);
  expect(perf.memoryMB.rss).toBeLessThan(300);
  expect(Math.min(...counts)).toBeGreaterThan(200);
  expect(heatMs).toBeLessThan(1000);
});
