// oft-archive: the only gateway to the private "oft-archive" Storage bucket.
// Auth: header x-oft-key must equal oft.secrets['archive_key'] (read via the function's own DB URL).
// The Supabase service key never leaves Supabase: it is injected into this function by the platform.
import { createClient } from 'npm:@supabase/supabase-js@2';
import postgres from 'npm:postgres@3';

const BUCKET = 'oft-archive';
const sql = postgres(Deno.env.get('SUPABASE_DB_URL')!, { max: 1, prepare: false });
const storage = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!, { auth: { persistSession: false } }).storage.from(BUCKET);
let cachedKey: { v: string; t: number } | null = null;

async function key(): Promise<string> {
  if (cachedKey && Date.now() - cachedKey.t < 300_000) return cachedKey.v;
  const r = await sql`select v from oft.secrets where k = 'archive_key'`;
  cachedKey = { v: r[0]?.v ?? '', t: Date.now() };
  return cachedKey.v;
}

function eq(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { 'content-type': 'application/json' } });

Deno.serve(async (req) => {
  try {
    if (!eq(req.headers.get('x-oft-key') ?? '', await key())) return json({ error: 'unauthorized' }, 401);
    const u = new URL(req.url);
    const path = u.searchParams.get('path') ?? '';
    if (u.searchParams.get('op') === 'usage') {
      const r = await sql`select count(*)::bigint as objects, coalesce(sum((metadata->>'size')::bigint),0)::bigint as bytes from storage.objects where bucket_id = ${BUCKET}`;
      return json({ objects: Number(r[0].objects), bytes: Number(r[0].bytes) });
    }
    if (!/^[a-z0-9._\/-]{3,200}$/i.test(path) || path.includes('..')) return json({ error: 'bad path' }, 400);
    if (req.method === 'PUT') {
      const body = new Uint8Array(await req.arrayBuffer());
      const { error } = await storage.upload(path, body, { contentType: 'application/gzip', upsert: false });
      if (error && !/exists|Duplicate/i.test(error.message)) return json({ error: error.message }, 500);
      return json({ ok: true, bytes: body.length, existed: !!error });
    }
    if (req.method === 'GET' && u.searchParams.get('op') === 'exists') {
      const dir = path.split('/').slice(0, -1).join('/');
      const name = path.split('/').pop()!;
      const { data, error } = await storage.list(dir, { search: name, limit: 10 });
      if (error) return json({ error: error.message }, 500);
      const f = (data ?? []).find((x) => x.name === name);
      return json({ exists: !!f, size: f?.metadata?.size ?? null });
    }
    if (req.method === 'GET') {
      const { data, error } = await storage.download(path);
      if (error || !data) return json({ error: error?.message ?? 'not found' }, 404);
      return new Response(data, { headers: { 'content-type': 'application/gzip' } });
    }
    if (req.method === 'DELETE') {
      const { error } = await storage.remove([path]);
      if (error) return json({ error: error.message }, 500);
      return json({ ok: true });
    }
    return json({ error: 'method' }, 405);
  } catch (e) {
    return json({ error: String((e as Error).message ?? e) }, 500);
  }
});
