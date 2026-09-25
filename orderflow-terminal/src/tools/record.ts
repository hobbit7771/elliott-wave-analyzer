// Record a live normalized stream from Binance into NDJSON for deterministic replay / regression tests.
//   node dist/tools/record.js --source binance-futures --symbol BTCUSDT --seconds 120 --out recordings/btc.ndjson
import { createWriteStream, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { getAdapter, isSource } from '../server/adapters/registry.js';
import { ReconnectingWs } from '../server/ingestion/wsClient.js';
import { autoHeatStep } from '../core/heatmap.js';

const arg = (k: string, d: string): string => {
  const i = process.argv.indexOf('--' + k);
  return i > 0 ? process.argv[i + 1] : d;
};
const source = arg('source', 'binance-futures');
const symbol = arg('symbol', 'BTCUSDT').toUpperCase();
const seconds = +arg('seconds', '60');
const out = arg('out', `recordings/${symbol}-${Date.now()}.ndjson`);
if (!isSource(source)) throw new Error('bad source');
const ad = getAdapter(source);
const meta = (await ad.listInstruments()).find((m) => m.symbol === symbol);
if (!meta) throw new Error('unknown symbol');
mkdirSync(dirname(out), { recursive: true });
const w = createWriteStream(out);
const line = (o: unknown) => w.write(JSON.stringify(o) + '\n');
const klines = await ad.fetchKlines(symbol, '1m', 300);
let wroteMeta = false;
let diffs = 0;
let trades = 0;
const routes = ad.streamRoutes(symbol);
const sockets = routes.map(
  (r) =>
    new ReconnectingWs({
      url: r.url,
      onOpen: () => {
        if (r.route === 'flow') return;
        setTimeout(async () => {
          const snap = await ad.fetchSnapshot(symbol);
          if (!wroteMeta) {
            const range = snap.asks[snap.asks.length - 1][0] - snap.bids[snap.bids.length - 1][0];
            line({ k: 'meta', meta, syncMode: ad.syncMode, heatStep: autoHeatStep(meta.tickSize, range, 600) });
            line({ k: 'klines', c: klines });
            wroteMeta = true;
          }
          line({ k: 'snap', s: snap });
        }, 500);
      },
      onMessage: (raw) => {
        for (const m of ad.parse(raw)) {
          if (m.kind === 'diff') (line({ k: 'diff', d: m.d }), diffs++);
          else if (m.kind === 'trade') (line({ k: 'trade', tr: m.tr }), trades++);
        }
      },
    }),
);
// buffer early lines until meta is written: meta must come first for replay
const origWrite = w.write.bind(w);
const early: string[] = [];
w.write = ((chunk: string) => {
  if (!wroteMeta && !chunk.startsWith('{"k":"meta"')) {
    early.push(chunk);
    return true;
  }
  if (chunk.startsWith('{"k":"meta"')) {
    origWrite(chunk);
    return true;
  }
  if (early.length) for (const e of early.splice(0)) origWrite(e);
  return origWrite(chunk);
}) as typeof w.write;
const ticker = setInterval(() => wroteMeta && line({ k: 'tick', t: Date.now() }), 250);
for (const ws of sockets) ws.start();
setTimeout(() => {
  clearInterval(ticker);
  for (const ws of sockets) ws.stop();
  w.end(() => {
    console.log(`recorded ${diffs} depth diffs and ${trades} trades to ${out}`);
    process.exit(0);
  });
}, seconds * 1000);
