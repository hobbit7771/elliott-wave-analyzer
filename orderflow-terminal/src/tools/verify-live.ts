// Live verification against the real exchange (or a deployed server).
//   node dist/tools/verify-live.js [--source binance-futures] [--symbol BTCUSDT] [--seconds 20]
//   node dist/tools/verify-live.js --server https://your-app.onrender.com --symbol BTCUSDT
import WebSocket from 'ws';
import { getAdapter, isSource } from '../server/adapters/registry.js';
import { BookSync, OrderBook } from '../core/orderbook.js';

const arg = (k: string, d: string): string => {
  const i = process.argv.indexOf('--' + k);
  return i > 0 ? process.argv[i + 1] : d;
};
const source = arg('source', 'binance-futures');
const symbol = arg('symbol', 'BTCUSDT').toUpperCase();
const seconds = +arg('seconds', '20');
const server = arg('server', '');
const ok = (m: string) => console.log('  ✔ ' + m);
const bad = (m: string) => {
  console.log('  ✘ ' + m);
  process.exitCode = 1;
};

if (!isSource(source)) throw new Error('bad source');

if (server) {
  console.log(`Verifying deployed server ${server} (${source} ${symbol})`);
  const h = await fetch(server + '/api/health').then((r) => r.json());
  h.ok ? ok('health ' + JSON.stringify(h)) : bad('health failed');
  const k = await fetch(`${server}/api/klines?source=${source}&symbol=${symbol}&tf=1m&limit=5`).then((r) => r.json());
  k.candles?.length ? ok(`klines: last close ${k.candles[k.candles.length - 1].c}`) : bad('klines: ' + JSON.stringify(k));
  const ws = new WebSocket(server.replace(/^http/, 'ws') + '/ws');
  const seen = new Map<string, number>();
  let lastBook: { bids: [number, number][]; asks: [number, number][] } | null = null;
  let lastStatus: Record<string, unknown> | null = null;
  ws.on('open', () => ws.send(JSON.stringify({ op: 'sub', source, symbol })));
  ws.on('message', (raw) => {
    const m = JSON.parse(raw.toString());
    seen.set(m.ch, (seen.get(m.ch) ?? 0) + 1);
    if (m.ch === 'book') lastBook = m.d;
    if (m.ch === 'status') lastStatus = m.d;
  });
  await new Promise((r) => setTimeout(r, seconds * 1000));
  ws.close();
  console.log('  messages by channel:', Object.fromEntries(seen));
  const b = lastBook as { bids: [number, number][]; asks: [number, number][] } | null;
  b && b.bids.length && b.asks.length && b.bids[0][0] < b.asks[0][0] ? ok(`book: best bid ${b.bids[0][0]} / best ask ${b.asks[0][0]}, ${b.bids.length}+${b.asks.length} levels`) : bad('no valid book received');
  (seen.get('trades') ?? 0) > 0 ? ok('trades streaming') : bad('no trades received');
  const st = lastStatus as Record<string, unknown> | null;
  st?.state === 'connected' ? ok(`status connected, latency ${st.latencyMs} ms, gaps ${st.gaps}, resyncs ${st.resyncs}`) : bad('status: ' + JSON.stringify(st));
  process.exit();
}

const ad = getAdapter(source);
console.log(`Verifying ${ad.name} ${symbol} directly (${seconds}s)`);
const inst = (await ad.listInstruments()).find((m) => m.symbol === symbol);
inst ? ok(`exchangeInfo: tick ${inst.tickSize}, step ${inst.stepSize}`) : bad('symbol not found');
const kl = await ad.fetchKlines(symbol, '1m', 3);
ok(`REST klines: ${kl.length} candles, last close ${kl[kl.length - 1].c}, taker-buy vol ${kl[kl.length - 1].bv}`);
const book = new OrderBook(inst!.tickSize);
const sync = new BookSync(ad.syncMode);
sync.reset();
let diffs = 0;
let trades = 0;
let gaps = 0;
let bboChecks = 0;
let bboMismatch = 0;
let lat = 0;
let marks = 0;
const sockets = ad.streamRoutes(symbol).map((r) => {
  const ws = new WebSocket(r.url);
  ws.on('message', (raw) => {
    for (const m of ad.parse(raw.toString())) {
      if (m.kind === 'diff') {
        diffs++;
        lat = Date.now() - m.eventTime;
        const res = sync.onDiff(m.d);
        if (res.gap) gaps++;
        for (const d of res.applied) book.applyDiff(d);
      } else if (m.kind === 'trade') trades++;
      else if (m.kind === 'mark') marks++;
      else if (m.kind === 'bbo' && sync.state === 'synced') {
        bboChecks++;
        if (Math.abs(book.bestBid - m.bid) > inst!.tickSize * 20 || Math.abs(book.bestAsk - m.ask) > inst!.tickSize * 20) bboMismatch++;
      }
    }
  });
  return { r, ws };
});
for (const { r, ws } of sockets) {
  await new Promise((res, rej) => (ws.once('open', res), ws.once('error', rej)));
  ok(`WebSocket (${r.route}) connected: ${r.url}`);
}
await new Promise((r) => setTimeout(r, 1500));
const snap = await ad.fetchSnapshot(symbol);
book.applySnapshot(snap);
const res = sync.onSnapshot(snap.lastUpdateId);
for (const d of res.applied) book.applyDiff(d);
res.gap ? bad('snapshot could not be bridged by buffered diffs: ' + res.reason) : ok(`snapshot ${snap.lastUpdateId} bridged, ${res.applied.length} buffered diffs applied`);
await new Promise((r) => setTimeout(r, seconds * 1000));
for (const { ws } of sockets) ws.close();
if (ad.caps.markPrice) marks > 0 ? ok(`${marks} markPrice updates (/market route)`) : bad('no markPrice updates');
diffs > 0 ? ok(`${diffs} depth diffs (${(diffs / (seconds + 1.5)).toFixed(1)}/s)`) : bad('no depth diffs');
trades > 0 ? ok(`${trades} aggTrades`) : bad('no trades (illiquid symbol?)');
gaps === 0 ? ok('sequence continuity: no gaps') : bad(`${gaps} sequence gaps`);
ok(`latency (wall - event time) of last diff: ${lat} ms`);
bboChecks > 0 && bboMismatch / bboChecks < 0.02 ? ok(`local book best bid/ask matches bookTicker in ${bboChecks - bboMismatch}/${bboChecks} checks`) : bad(`book vs bookTicker mismatches: ${bboMismatch}/${bboChecks}`);
console.log(`  best bid ${book.bestBid} / best ask ${book.bestAsk}, ${book.size} levels`);
process.exit();
