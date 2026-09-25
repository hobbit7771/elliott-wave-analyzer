// Server performance under a continuous stream (10 depth diffs/s + trades) with a browser-like client.
import WebSocket from 'ws';
import { startTestVenue } from './testVenue.js';
import { ARCHIVE_KEY, OWNER, startServer } from './harness.js';
import { startPg, startArchiveServer } from '../fixtures/pg.js';

it('keeps event-loop lag and memory low while ingesting, recording and fanning out', async () => {
  const pg = await startPg();
  const arch = await startArchiveServer(ARCHIVE_KEY);
  const v = await startTestVenue();
  const srv = await startServer({ venueUrl: v.url, venueWs: v.wsUrl, pgPort: pg.port, archiveUrl: arch.url });
  const port = srv.port;
  // three clients subscribed to the live stream
  const counts = [0, 0, 0];
  const clients = counts.map((_, i) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws`);
    ws.on('open', () => ws.send(JSON.stringify({ op: 'sub', source: 'binance-futures', symbol: 'TESTUSDT' })));
    ws.on('message', () => counts[i]++);
    return ws;
  });
  await new Promise((r) => setTimeout(r, 30_000));
  const perf = await (await fetch(`http://127.0.0.1:${port}/api/perf`, { headers: { 'x-oft-owner': OWNER } })).json();
  const t0 = performance.now();
  const hm = await (await fetch(`http://127.0.0.1:${port}/api/heatmap?source=binance-futures&symbol=TESTUSDT&from=${Date.now() - 900_000}`)).json();
  const heatMs = performance.now() - t0;
  const st = perf.hub.sessions[0].status;
  console.log(JSON.stringify({ memoryMB: perf.memoryMB, eventLoopLagMs: perf.eventLoopLagMs, db: perf.db, msgsPerClient: counts, heatmapQueryMs: Math.round(heatMs), heatCols: hm.cols.length, feed: { trades: st.trades, depthUpdates: st.depthUpdates, gaps: st.gaps, dropped: st.dropped, latencyMs: st.latencyMs } }, null, 1));
  for (const c of clients) c.close();
  await srv.stop();
  await v.close();
  await arch.stop();
  await pg.stop();
  expect(st.state).toBe('connected');
  expect(st.gaps).toBe(0);
  expect(perf.eventLoopLagMs.p99).toBeLessThan(50);
  expect(perf.memoryMB.rss).toBeLessThan(300);
  expect(Math.min(...counts)).toBeGreaterThan(200);
  expect(heatMs).toBeLessThan(1000);
});
