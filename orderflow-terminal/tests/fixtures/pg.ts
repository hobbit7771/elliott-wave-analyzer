// TEST-ONLY: a real Postgres (PGlite, WASM) behind a local socket, with the oft schema applied.
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';
import { PGLiteSocketServer } from '@electric-sql/pglite-socket';
import postgres from 'postgres';

export async function startPg(): Promise<{ sql: postgres.Sql; stop: () => Promise<void> }> {
  const db = await PGlite.create();
  // Supabase-specific statements (API roles, storage schema) do not exist in plain Postgres
  const ddl = readFileSync('supabase/migrations/0001_oft_init.sql', 'utf8')
    .split('\n')
    .filter((l) => !/anon|authenticated|storage\.buckets|values \('oft-archive'|on conflict \(id\) do nothing/.test(l))
    .join('\n');
  await db.exec(ddl);
  const srv = new PGLiteSocketServer({ db, port: 0, host: '127.0.0.1' });
  await srv.start();
  const port = (srv as unknown as { port?: number; server?: { address(): { port: number } } }).port ?? (srv as unknown as { server: { address(): { port: number } } }).server.address().port;
  const sql = postgres({ host: '127.0.0.1', port, database: 'postgres', username: 'postgres', password: 'x', max: 1, prepare: false, onnotice: () => {} });
  return {
    sql,
    stop: async () => {
      await sql.end({ timeout: 1 });
      await srv.stop();
      await db.close();
    },
  };
}

/** In-memory stand-in for the oft-archive edge function (Storage). */
export class FakeArchive {
  objects = new Map<string, Uint8Array>();
  failPuts = 0;
  puts = 0;
  async put(path: string, body: Uint8Array) {
    this.puts++;
    if (this.failPuts > 0) {
      this.failPuts--;
      throw new Error('storage unavailable (test)');
    }
    const existed = this.objects.has(path);
    if (!existed) this.objects.set(path, body);
    return { bytes: body.length, existed };
  }
  async exists(path: string) {
    const o = this.objects.get(path);
    return { exists: !!o, size: o ? o.length : null };
  }
  async get(path: string) {
    const o = this.objects.get(path);
    if (!o) throw new Error('not found');
    return o;
  }
  async del(path: string) {
    this.objects.delete(path);
  }
  async usage() {
    let bytes = 0;
    for (const o of this.objects.values()) bytes += o.length;
    return { objects: this.objects.size, bytes };
  }
}
