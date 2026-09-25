// Client for the oft-archive Supabase Edge Function (private Storage bucket).
export interface ArchiveConfig {
  url: string; // https://<ref>.supabase.co/functions/v1/oft-archive
  key: string;
}

export function archiveConfigFromEnv(env = process.env): ArchiveConfig | null {
  const ref = env.SUPABASE_PROJECT_REF;
  const key = env.OFT_ARCHIVE_KEY;
  if (!key || (!ref && !env.OFT_ARCHIVE_URL)) return null;
  return { url: env.OFT_ARCHIVE_URL ?? `https://${ref}.supabase.co/functions/v1/oft-archive`, key };
}

export interface ArchiveClient {
  put(path: string, body: Uint8Array): Promise<{ bytes: number; existed: boolean }>;
  exists(path: string): Promise<{ exists: boolean; size: number | null }>;
  get(path: string): Promise<Uint8Array>;
  del(path: string): Promise<void>;
  usage(): Promise<{ objects: number; bytes: number }>;
}

export function archiveClient(cfg: ArchiveConfig, fetchImpl: typeof fetch = fetch): ArchiveClient {
  const call = async (method: string, q: Record<string, string>, body?: Uint8Array): Promise<Response> => {
    const u = new URL(cfg.url);
    for (const [k, v] of Object.entries(q)) u.searchParams.set(k, v);
    const ac = new AbortController();
    const to = setTimeout(() => ac.abort(), 60_000);
    try {
      const r = await fetchImpl(u, { method, body: body ? Buffer.from(body) : undefined, headers: { 'x-oft-key': cfg.key }, signal: ac.signal });
      if (!r.ok) throw new Error(`archive ${method} ${q.path ?? q.op}: ${r.status} ${(await r.text()).slice(0, 200)}`);
      return r;
    } finally {
      clearTimeout(to);
    }
  };
  return {
    async put(path, body) {
      return (await (await call('PUT', { path }, body)).json()) as { bytes: number; existed: boolean };
    },
    async exists(path) {
      return (await (await call('GET', { path, op: 'exists' })).json()) as { exists: boolean; size: number | null };
    },
    async get(path) {
      return new Uint8Array(await (await call('GET', { path })).arrayBuffer());
    },
    async del(path) {
      await call('DELETE', { path });
    },
    async usage() {
      return (await (await call('GET', { op: 'usage', path: 'usage' })).json()) as { objects: number; bytes: number };
    },
  };
}
