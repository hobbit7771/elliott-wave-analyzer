// Supabase Postgres connection (least-privilege role oft_app through the Supavisor pooler).
import postgres from 'postgres';

export type Sql = postgres.Sql;

export interface DbConfig {
  /** full connection URL (tests / self-hosted Postgres); otherwise the Supabase pooler is used */
  url?: string;
  projectRef: string;
  password: string;
  user: string;
  /** explicit pooler host(s); otherwise the regional candidates are tried in order */
  hosts: string[];
  port: number;
}

export function dbConfigFromEnv(env = process.env): DbConfig | null {
  if (env.OFT_DB_URL) return { url: env.OFT_DB_URL, projectRef: '', password: '', user: '', hosts: [], port: 0 };
  const ref = env.SUPABASE_PROJECT_REF;
  const password = env.OFT_DB_PASSWORD;
  if (!ref || !password) return null;
  const region = env.SUPABASE_REGION ?? 'eu-central-1';
  const hosts = env.OFT_DB_HOST ? env.OFT_DB_HOST.split(',') : [`aws-0-${region}.pooler.supabase.com`, `aws-1-${region}.pooler.supabase.com`];
  return { projectRef: ref, password, user: env.OFT_DB_USER ?? 'oft_app', hosts, port: +(env.OFT_DB_PORT ?? 5432) };
}

/** Connect to the first pooler host that accepts the role. Returns the client and the host used. */
export async function connectDb(cfg: DbConfig, log: (m: string) => void, max = 2): Promise<{ sql: Sql; host: string }> {
  if (cfg.url) {
    const sql = postgres(cfg.url, { max, idle_timeout: 60, connect_timeout: 10, prepare: false, onnotice: () => {} });
    await sql`select 1`;
    const host = new URL(cfg.url).host;
    log(`database: connected via ${host}`);
    return { sql, host };
  }
  let last: unknown;
  for (const host of cfg.hosts) {
    const sql = postgres({
      host,
      port: cfg.port,
      database: 'postgres',
      username: `${cfg.user}.${cfg.projectRef}`,
      password: cfg.password,
      ssl: 'require',
      max,
      idle_timeout: 60,
      connect_timeout: 10,
      prepare: false,
      onnotice: () => {},
    });
    try {
      await sql`select 1`;
      log(`supabase: connected via ${host}`);
      return { sql, host };
    } catch (e) {
      last = e;
      log(`supabase: ${host} failed: ${(e as Error).message}`);
      await sql.end({ timeout: 1 }).catch(() => {});
    }
  }
  throw last ?? new Error('no pooler host');
}
