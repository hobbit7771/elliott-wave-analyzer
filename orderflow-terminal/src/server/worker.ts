// Ingestion worker thread: one per live instrument. Keeps WebSocket parsing, book reconstruction,
// detectors, local SQLite cache and Supabase writes off the main (HTTP/WebSocket-serving) thread.
import { parentPort, workerData } from 'node:worker_threads';
import type { InstrumentMeta } from '../core/types.js';
import type { DetectorConfig } from '../core/detectors/config.js';
import { getAdapter } from './adapters/registry.js';
import { Recorder, openDb, symKey } from './recorder.js';
import { Session } from './session.js';
import { PersistWriter } from './persist/writer.js';
import { connectDb, dbConfigFromEnv } from './persist/db.js';
import { PgRepo } from './persist/repo.js';
import { archiveClient, archiveConfigFromEnv } from './persist/archive.js';

interface Init {
  meta: InstrumentMeta;
  cfg: DetectorConfig;
  dbPath: string;
  backfillMinutes: number;
}

const init = workerData as Init;
const port = parentPort!;
const key = symKey(init.meta.source, init.meta.symbol);
const log = (m: string) => port.postMessage({ op: 'log', m });
const db = openDb(init.dbPath);
const recorder = new Recorder(db, key);
const adapter = getAdapter(init.meta.source);

let persist: PersistWriter | undefined;
const dbCfg = dbConfigFromEnv();
if (dbCfg) {
  const arch = archiveConfigFromEnv();
  persist = new PersistWriter(init.meta, null, arch ? archiveClient(arch) : null, log, undefined, true);
  persist.start();
  const attach = async (attempt: number): Promise<void> => {
    try {
      const { sql } = await connectDb(dbCfg, log, 1);
      persist!.attach(new PgRepo(sql));
    } catch (e) {
      const wait = Math.min(300_000, 10_000 * 2 ** attempt);
      log(`[${key}] Supabase unavailable: ${(e as Error).message} — history is NOT being written, retry in ${wait / 1000}s`);
      setTimeout(() => void attach(attempt + 1), wait);
    }
  };
  void attach(0);
} else log(`[${key}] SUPABASE_PROJECT_REF / OFT_DB_PASSWORD not set — persistent history disabled`);

const session = new Session(init.meta, adapter, init.cfg, {
  recorder,
  persist,
  backfillMinutes: init.backfillMinutes,
  log,
  // serialize once here; the main thread fans the string out to all subscribers unchanged
  publish: (ch, d) => port.postMessage({ op: 'pub', ch, s: JSON.stringify({ ch, k: key, d }) }),
});

port.on('message', async (msg: { op: string; cfg?: DetectorConfig; policy?: { archive: boolean; heat10: boolean } }) => {
  if (msg.op === 'config' && msg.cfg) session.setConfig(msg.cfg);
  else if (msg.op === 'policy' && msg.policy) persist?.setPolicy(msg.policy);
  else if (msg.op === 'stop') {
    await session.stop();
    db.close();
    port.postMessage({ op: 'stopped' });
    port.close();
  }
});

void session.start();
