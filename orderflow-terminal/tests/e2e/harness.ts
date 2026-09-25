// TEST-ONLY: launches the built server against the local test venue, a real Postgres (PGlite) and the
// archive stub, the same way Render runs it against Binance + Supabase.
import { spawn, type ChildProcess } from 'node:child_process';
import { rmSync } from 'node:fs';
import { freePort } from './testVenue.js';

export const OWNER = 'test-owner';
export const ARCHIVE_KEY = 'test-archive-key';

export interface ServerHandle {
  base: string;
  port: number;
  proc: ChildProcess;
  log: string[];
  stop: () => Promise<void>;
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function startServer(o: { venueUrl: string; venueWs: string; pgPort: number; archiveUrl: string; dbPath?: string; extra?: Record<string, string> }): Promise<ServerHandle> {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const dbPath = o.dbPath ?? `/tmp/oft-e2e-${port}.sqlite`;
  rmSync(dbPath, { force: true });
  const proc = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', 'dist/server/index.js'], {
    env: {
      ...process.env,
      NODE_ENV: 'test',
      PORT: String(port),
      HOST: '127.0.0.1',
      DB_PATH: dbPath,
      BINANCE_FUTURES_REST: o.venueUrl,
      BINANCE_FUTURES_WS_BASE: o.venueWs,
      DEFAULT_SYMBOLS: 'binance-futures:TESTUSDT',
      BACKFILL_MINUTES: '1',
      OFT_DB_URL: `postgres://postgres:x@127.0.0.1:${o.pgPort}/postgres`,
      OFT_ARCHIVE_URL: o.archiveUrl,
      OFT_ARCHIVE_KEY: ARCHIVE_KEY,
      OWNER_TOKEN: OWNER,
      ARCHIVE_BLOCK_MS: '10000',
      ...o.extra,
    },
  });
  const log: string[] = [];
  proc.stdout?.on('data', (d) => log.push(String(d)));
  proc.stderr?.on('data', (d) => log.push(String(d)));
  let up = false;
  for (let i = 0; i < 150 && !up; i++) {
    try {
      const h = (await (await fetch(base + '/api/health')).json()) as { supabase?: boolean };
      up = h.supabase === true;
    } catch {
      /* not up yet */
    }
    if (!up) await wait(200);
  }
  if (!up) {
    proc.kill('SIGKILL');
    throw new Error('server did not start:\n' + log.join(''));
  }
  return {
    base,
    port,
    proc,
    log,
    stop: async () => {
      if (proc.exitCode !== null) return;
      const exited = new Promise((r) => proc.once('exit', r));
      proc.kill('SIGTERM');
      await exited;
      rmSync(dbPath, { force: true });
    },
  };
}
