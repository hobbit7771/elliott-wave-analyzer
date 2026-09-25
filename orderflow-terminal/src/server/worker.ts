// Ingestion worker thread: one per live instrument. Keeps WebSocket parsing, book reconstruction,
// detectors and SQLite writes off the main (HTTP/WebSocket-serving) thread.
import { parentPort, workerData } from 'node:worker_threads';
import type { InstrumentMeta } from '../core/types.js';
import type { DetectorConfig } from '../core/detectors/config.js';
import { getAdapter } from './adapters/registry.js';
import { Recorder, openDb, symKey } from './recorder.js';
import { Session } from './session.js';

interface Init {
  meta: InstrumentMeta;
  cfg: DetectorConfig;
  dbPath: string;
  backfillMinutes: number;
}

const init = workerData as Init;
const port = parentPort!;
const key = symKey(init.meta.source, init.meta.symbol);
const db = openDb(init.dbPath);
const recorder = new Recorder(db, key);
const adapter = getAdapter(init.meta.source);

const session = new Session(init.meta, adapter, init.cfg, {
  recorder,
  backfillMinutes: init.backfillMinutes,
  log: (m) => port.postMessage({ op: 'log', m }),
  // serialize once here; the main thread fans the string out to all subscribers unchanged
  publish: (ch, d) => port.postMessage({ op: 'pub', ch, s: JSON.stringify({ ch, k: key, d }) }),
});

port.on('message', (msg: { op: string; cfg?: DetectorConfig }) => {
  if (msg.op === 'config' && msg.cfg) session.setConfig(msg.cfg);
  else if (msg.op === 'stop') {
    session.stop();
    db.close();
    port.postMessage({ op: 'stopped' });
    port.close();
  }
});

void session.start();
