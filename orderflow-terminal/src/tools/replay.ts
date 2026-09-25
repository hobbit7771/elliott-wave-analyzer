// Replay an NDJSON recording through the detection engine and print what was detected.
//   node dist/tools/replay.js recordings/btc.ndjson [--json]
import { readFileSync } from 'node:fs';
import { parseNdjson, replay } from '../core/replay.js';

const file = process.argv[2];
if (!file) {
  console.error('usage: replay <file.ndjson> [--json]');
  process.exit(2);
}
const t0 = performance.now();
const r = replay(parseNdjson(readFileSync(file, 'utf8')));
const ms = performance.now() - t0;
if (process.argv.includes('--json')) console.log(JSON.stringify(r.events, null, 1));
else {
  console.log(`replayed ${r.diffs} diffs, ${r.trades} trades, ${r.columns.length} heat columns, ${r.gaps} gaps in ${ms.toFixed(0)} ms`);
  const by = new Map<string, number>();
  for (const e of r.events) by.set(e.kind, (by.get(e.kind) ?? 0) + 1);
  console.log('events by kind:', Object.fromEntries(by));
  for (const e of r.events.slice(-20)) console.log(new Date(e.t).toISOString(), e.kind, e.side ?? '', e.price, `conf ${e.confidence}`, '-', e.title);
}
