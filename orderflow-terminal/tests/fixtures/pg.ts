// TEST-ONLY: a real Postgres (PGlite, WASM) behind a local socket, with the oft schema applied.
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';
import net from 'node:net';
import postgres from 'postgres';

/**
 * Minimal Postgres wire server over one PGlite instance. PGlite is a single session, so requests from
 * different client connections are serialized: a client holds the lock from its batch (up to Sync/Query)
 * until PGlite reports ReadyForQuery with status idle — transactions and extended-protocol batches from
 * other connections never interleave (pglite-socket's multiplexing does interleave them).
 */
function serveWire(db: PGlite): net.Server {
  let holder: net.Socket | null = null;
  const waiters: (() => void)[] = [];
  const acquire = async (s: net.Socket) => {
    while (holder && holder !== s) await new Promise<void>((r) => waiters.push(r));
    holder = s;
  };
  const release = (s: net.Socket) => {
    if (holder !== s) return;
    holder = null;
    waiters.splice(0).forEach((w) => w());
  };
  const msg = (type: string, body: Buffer) => {
    const h = Buffer.alloc(5);
    h.write(type, 0);
    h.writeInt32BE(body.length + 4, 1);
    return Buffer.concat([h, body]);
  };
  const param = (k: string, v: string) => msg('S', Buffer.from(`${k}\0${v}\0`));
  const int32s = (...n: number[]) => {
    const b = Buffer.alloc(4 * n.length);
    n.forEach((x, i) => b.writeInt32BE(x, 4 * i));
    return b;
  };
  const socks = new Set<net.Socket>();
  const srv = net.createServer((sock) => {
    socks.add(sock);
    sock.on('close', () => socks.delete(sock));
    let buf = Buffer.alloc(0);
    let started = false;
    let batch: Buffer[] = [];
    let chain = Promise.resolve();
    sock.on('error', () => {});
    sock.on('close', () => release(sock));
    sock.on('data', (d) => {
      buf = Buffer.concat([buf, d]);
      chain = chain.then(async () => {
        for (;;) {
          if (!started) {
            if (buf.length < 8) return;
            const len = buf.readInt32BE(0);
            if (buf.length < len) return;
            const code = buf.readInt32BE(4);
            buf = buf.subarray(len);
            if (code === 80877103) {
              sock.write('N'); // no SSL
              continue;
            }
            started = true;
            sock.write(
              Buffer.concat([
                msg('R', int32s(0)),
                param('server_version', '16.0'),
                param('client_encoding', 'UTF8'),
                param('DateStyle', 'ISO, MDY'),
                param('integer_datetimes', 'on'),
                param('standard_conforming_strings', 'on'),
                param('TimeZone', 'UTC'),
                msg('K', int32s(1, 1)),
                msg('Z', Buffer.from('I')),
              ]),
            );
            continue;
          }
          if (buf.length < 5) return;
          const len = buf.readInt32BE(1) + 1;
          if (buf.length < len) return;
          const m = buf.subarray(0, len);
          const type = String.fromCharCode(m[0]);
          buf = buf.subarray(len);
          if (type === 'X') {
            release(sock);
            sock.end();
            return;
          }
          batch.push(m);
          if (type !== 'S' && type !== 'Q' && type !== 'H') continue;
          const req = Buffer.concat(batch);
          batch = [];
          await acquire(sock);
          const out = Buffer.from(await db.execProtocolRaw(new Uint8Array(req), { syncToFs: false }));
          if (!sock.destroyed) sock.write(out);
          // last message is ReadyForQuery ('Z', len 5, status): keep the lock while in a transaction
          const z = out.length >= 6 && out[out.length - 6] === 0x5a ? String.fromCharCode(out[out.length - 1]) : 'I';
          if (type !== 'H' && z === 'I') release(sock);
        }
      });
    });
  });
  srv.on('drain-all', () => socks.forEach((x) => x.destroy()));
  return srv;
}

export async function startPg(): Promise<{ sql: postgres.Sql; port: number; stop: () => Promise<void> }> {
  const db = await PGlite.create();
  // Supabase-specific statements (API roles, storage schema) do not exist in plain Postgres
  const ddl = readFileSync('supabase/migrations/0001_oft_init.sql', 'utf8')
    .split('\n')
    .filter((l) => !/anon|authenticated|storage\.buckets|values \('oft-archive'|on conflict \(id\) do nothing/.test(l))
    .join('\n');
  await db.exec(ddl);
  const srv = serveWire(db);
  await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
  const port = (srv.address() as net.AddressInfo).port;
  const sql = postgres({ host: '127.0.0.1', port, database: 'postgres', username: 'postgres', password: 'x', max: 1, prepare: false, onnotice: () => {} });
  return {
    sql,
    port,
    stop: async () => {
      await sql.end({ timeout: 1 });
      const closed = new Promise<void>((r) => srv.close(() => r()));
      srv.emit('drain-all');
      await closed;
      await db.close().catch(() => {});
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

/** TEST-ONLY HTTP stand-in for the oft-archive edge function (same API, in-memory objects). */
export async function startArchiveServer(key: string): Promise<{ url: string; fake: FakeArchive; stop: () => Promise<void> }> {
  const http = await import('node:http');
  const fake = new FakeArchive();
  const srv = http.createServer(async (req, res) => {
    if (req.headers['x-oft-key'] !== key) return void res.writeHead(401).end('{"error":"unauthorized"}');
    const u = new URL(req.url ?? '/', 'http://x');
    const path = u.searchParams.get('path') ?? '';
    const json = (b: unknown, code = 200) => (res.writeHead(code, { 'content-type': 'application/json' }), res.end(JSON.stringify(b)));
    try {
      if (u.searchParams.get('op') === 'usage') return json(await fake.usage());
      if (req.method === 'PUT') {
        const chunks: Buffer[] = [];
        for await (const c of req) chunks.push(c as Buffer);
        return json(await fake.put(path, new Uint8Array(Buffer.concat(chunks))));
      }
      if (req.method === 'GET' && u.searchParams.get('op') === 'exists') return json(await fake.exists(path));
      if (req.method === 'GET') {
        const b = await fake.get(path);
        res.writeHead(200, { 'content-type': 'application/gzip' });
        return void res.end(Buffer.from(b));
      }
      if (req.method === 'DELETE') return json((await fake.del(path), { ok: true }));
      json({ error: 'method' }, 405);
    } catch (e) {
      json({ error: (e as Error).message }, 404);
    }
  });
  await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
  const port = (srv.address() as { port: number }).port;
  return { url: `http://127.0.0.1:${port}/archive`, fake, stop: () => new Promise((r) => srv.close(() => r())) };
}
