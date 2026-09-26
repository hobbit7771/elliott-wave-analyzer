// A client subscribing to a Bybit instrument over /ws gets the live book and trades (the server once ignored
// every source except Binance in the subscription and the UI stayed at "connecting").
import WebSocket from 'ws';
import { startTestVenue } from './testVenue.js';
import { ARCHIVE_KEY, startServer } from './harness.js';
import { startPg, startArchiveServer } from '../fixtures/pg.js';
import { FakeBybit } from '../fixtures/fakeBybit.js';

it('streams a Bybit instrument to a subscribed client', async () => {
  const pg = await startPg();
  const arch = await startArchiveServer(ARCHIVE_KEY);
  const v = await startTestVenue();
  const bybit = new FakeBybit('TESTUSDT');
  await bybit.listen();
  const srv = await startServer({ venueUrl: v.url, venueWs: v.wsUrl, pgPort: pg.port, archiveUrl: arch.url, extra: { BYBIT_REST: bybit.restUrl, BYBIT_WS: bybit.url } });
  const got: { ch: string; d: unknown }[] = [];
  const ws = new WebSocket(`ws://127.0.0.1:${srv.port}/ws`);
  ws.on('open', () => ws.send(JSON.stringify({ op: 'sub', source: 'bybit-linear', symbol: 'TESTUSDT' })));
  ws.on('message', (m) => got.push(JSON.parse(String(m))));
  const pump = setInterval(() => {
    bybit.delta([['100.0', String(3 + Math.random())]], []);
    bybit.trade(Math.random() > 0.5 ? 'Buy' : 'Sell', '100.1', '0.5');
  }, 200);
  const deadline = Date.now() + 30_000;
  const has = (ch: string) => got.some((m) => m.ch === ch);
  while (Date.now() < deadline && !(has('subscribed') && has('book') && has('trades'))) await new Promise((r) => setTimeout(r, 200));
  clearInterval(pump);
  const errors = got.filter((m) => m.ch === 'error');
  const channels = [...new Set(got.map((m) => m.ch))];
  ws.close();
  await srv.stop();
  await bybit.close();
  await v.close();
  await arch.stop();
  await pg.stop();
  expect(errors).toEqual([]);
  expect(channels).toEqual(expect.arrayContaining(['subscribed', 'book']));
  expect(channels.some((c) => c.startsWith('trade'))).toBe(true);
  expect(bybit.subscribes).toEqual(expect.arrayContaining(['orderbook.200.TESTUSDT', 'publicTrade.TESTUSDT']));
}, 90_000);
