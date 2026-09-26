// Data access for the oft schema. All writes are idempotent upserts keyed by natural keys.
import { deflateSync, inflateSync } from 'node:zlib';
import type { Candle, HeatColumn, MarketEvent } from '../../core/types.js';
import type { Sql } from './db.js';

export interface ArchiveManifest {
  path: string;
  source: string;
  symbol: string;
  kind: string;
  schema_version: number;
  t0: number;
  t1: number;
  first_update_id: number | null;
  last_update_id: number | null;
  first_trade_id: number | null;
  last_trade_id: number | null;
  diffs: number;
  trades: number;
  depth_band: number;
  bytes: number;
  sha256: string;
  status: 'pending' | 'final';
}

export interface GapRow {
  source: string;
  symbol: string;
  stream: string;
  t0: number;
  t1: number | null;
  reason: string;
}

/** Everything the writer / readers need; implemented by PgRepo and by an in-memory fake in tests. */
export interface Repo {
  upsertCandles(source: string, symbol: string, rows: { c: Candle; origin: string }[]): Promise<void>;
  upsertHeat(source: string, symbol: string, res: number, cols: HeatColumn[]): Promise<void>;
  upsertEvents(rows: { ev: MarketEvent; detectedAt: number; algo: string }[]): Promise<void>;
  upsertGaps(rows: GapRow[]): Promise<void>;
  upsertCoverage(source: string, symbol: string, stream: string, t0: number, t1: number): Promise<void>;
  upsertManifest(m: ArchiveManifest): Promise<void>;
  finalizeManifest(path: string): Promise<void>;
  deleteManifest(path: string): Promise<void>;
  pendingManifests(olderThanMs: number): Promise<ArchiveManifest[]>;
  manifests(source: string, symbol: string, from: number, to: number): Promise<ArchiveManifest[]>;
  /** at most `limit` tiles, evenly thinned over the range */
  heat(source: string, symbol: string, res: number, from: number, to: number, limit: number): Promise<HeatColumn[]>;
  events(source: string, symbol: string, from: number, to: number, limit: number, asOf?: number): Promise<MarketEvent[]>;
  candles(source: string, symbol: string, from: number, to: number): Promise<(Candle & { origin: string })[]>;
  gaps(source: string, symbol: string, from: number, to: number): Promise<GapRow[]>;
  coverage(source: string, symbol: string): Promise<{ stream: string; t0: number; t1: number }[]>;
  putInstrument(meta: { source: string; symbol: string }): Promise<void>;
  upsertKlines(source: string, symbol: string, tf: string, rows: Candle[]): Promise<void>;
  klines(source: string, symbol: string, tf: string, from: number, to: number): Promise<Candle[]>;
  getInstrument<T>(source: string, symbol: string): Promise<T | undefined>;
  listInstruments<T>(source: string): Promise<T[]>;
  getSetting<T>(k: string): Promise<T | undefined>;
  setSetting(k: string, v: unknown): Promise<void>;
  recordList(): Promise<{ source: string; symbol: string; enabled: boolean }[]>;
  setRecord(source: string, symbol: string, enabled: boolean): Promise<void>;
  getPaper(account: string): Promise<unknown | undefined>;
  setPaper(account: string, state: unknown): Promise<void>;
  logAlert(row: { t: number; ruleId: number; source: string; symbol: string; eventId: string; body: unknown }): Promise<boolean>;
  alertLog(limit: number): Promise<{ t: number; rule_id: number; source: string; symbol: string; event_id: string; body: unknown }[]>;
  sizes(): Promise<{ dbBytes: number; tables: Record<string, number> }>;
  purge(policy: { heat10Ms: number; heat60Ms: number; eventsMs: number; candlesMs: number }, now: number): Promise<Record<string, number>>;
  deleteRange(source: string, symbol: string, from: number, to: number): Promise<Record<string, number>>;
}

const enc = (c: HeatColumn): Buffer => deflateSync(Buffer.from(JSON.stringify(c)));
const dec = (b: Uint8Array): HeatColumn => JSON.parse(inflateSync(b).toString());
const n = (v: unknown): number => (v === null || v === undefined ? NaN : Number(v));

export class PgRepo implements Repo {
  constructor(private sql: Sql) {}

  async upsertCandles(source: string, symbol: string, rows: { c: Candle; origin: string }[]): Promise<void> {
    if (!rows.length) return;
    const vals = rows.map(({ c, origin }) => ({ source, symbol, t: c.t, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v, bv: c.bv, n: c.n ?? null, origin }));
    await this.sql`insert into oft.candles_1m ${this.sql(vals)}
      on conflict (source, symbol, t) do update set o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v, bv=excluded.bv, n=excluded.n, origin=excluded.origin, stored_at=now()`;
  }

  async upsertHeat(source: string, symbol: string, res: number, cols: HeatColumn[]): Promise<void> {
    if (!cols.length) return;
    const vals = cols.map((c) => ({ source, symbol, res_s: res, t: c.t, observed_ms: Math.round(c.dt), step: c.step, data: enc(c) }));
    await this.sql`insert into oft.heat_tiles ${this.sql(vals)}
      on conflict (source, symbol, res_s, t) do update set observed_ms=excluded.observed_ms, step=excluded.step, data=excluded.data, stored_at=now()`;
  }

  async upsertEvents(rows: { ev: MarketEvent; detectedAt: number; algo: string }[]): Promise<void> {
    if (!rows.length) return;
    const statusAt = (e: MarketEvent) => e.endT ?? e.t;
    const all = rows.map(({ ev, detectedAt, algo }) => ({ source: ev.source, symbol: ev.symbol, id: ev.id, kind: ev.kind, t: ev.t, detected_at: detectedAt, status_at: Math.max(detectedAt, statusAt(ev)), score: ev.confidence, status: ev.status ?? null, algo_version: algo, body: this.sql.json(ev as never) }));
    // one row per id per statement (Postgres rejects double updates), keep the latest state
    const vals = [...new Map(all.map((v) => [v.id, v])).values()];
    // keep the earliest detection time on update (no look-ahead rewriting)
    await this.sql`insert into oft.events ${this.sql(vals)}
      on conflict (source, symbol, id) do update set kind=excluded.kind, status_at=excluded.status_at, score=excluded.score, status=excluded.status, body=excluded.body, stored_at=now()`;
    const hist = [...new Map(all.map((v) => [v.id + '|' + v.status_at, v])).values()].map((v) => ({ source: v.source, symbol: v.symbol, id: v.id, status_at: v.status_at, status: v.status, score: v.score, body: v.body }));
    await this.sql`insert into oft.event_status ${this.sql(hist)} on conflict do nothing`;
  }

  async upsertGaps(rows: GapRow[]): Promise<void> {
    if (!rows.length) return;
    await this.sql`insert into oft.gaps ${this.sql(rows as never)}
      on conflict (source, symbol, stream, t0) do update set t1=coalesce(excluded.t1, oft.gaps.t1), reason=excluded.reason`;
  }

  async upsertCoverage(source: string, symbol: string, stream: string, t0: number, t1: number): Promise<void> {
    await this.sql`insert into oft.coverage (source, symbol, stream, t0, t1) values (${source}, ${symbol}, ${stream}, ${t0}, ${t1})
      on conflict (source, symbol, stream, t0) do update set t1=greatest(oft.coverage.t1, excluded.t1)`;
  }

  async upsertManifest(m: ArchiveManifest): Promise<void> {
    await this.sql`insert into oft.archives ${this.sql(m as never)}
      on conflict (path) do update set t1=excluded.t1, bytes=excluded.bytes, sha256=excluded.sha256, diffs=excluded.diffs, trades=excluded.trades,
        last_update_id=excluded.last_update_id, last_trade_id=excluded.last_trade_id
      where oft.archives.status = 'pending'`;
  }
  async finalizeManifest(path: string): Promise<void> {
    await this.sql`update oft.archives set status='final', finalized_at=now() where path=${path}`;
  }
  async deleteManifest(path: string): Promise<void> {
    await this.sql`delete from oft.archives where path=${path}`;
  }
  async pendingManifests(olderThanMs: number): Promise<ArchiveManifest[]> {
    const rows = await this.sql`select * from oft.archives where status='pending' and created_at < now() - (${olderThanMs} || ' milliseconds')::interval`;
    return rows.map(normManifest);
  }
  async manifests(source: string, symbol: string, from: number, to: number): Promise<ArchiveManifest[]> {
    const rows = await this.sql`select * from oft.archives where source=${source} and symbol=${symbol} and status='final' and t1 >= ${from} and t0 <= ${to} order by t0 limit 500`;
    return rows.map(normManifest);
  }

  async heat(source: string, symbol: string, res: number, from: number, to: number, limit: number): Promise<HeatColumn[]> {
    // a decoded tile is ~10 KB in memory: thin out in SQL so a long range never loads thousands of tiles
    const [{ n: cnt, t0 }] = await this.sql`select count(*)::int as n, min(t) as t0 from oft.heat_tiles where source=${source} and symbol=${symbol} and res_s=${res} and t>=${from} and t<=${to}`;
    if (!cnt) return [];
    const stride = Math.max(1, Math.ceil(Number(cnt) / Math.max(1, limit)));
    const rows = await this.sql`select data from oft.heat_tiles where source=${source} and symbol=${symbol} and res_s=${res} and t>=${from} and t<=${to} and ((t - ${n(t0)}) / ${res * 1000}) % ${stride} = 0 order by t limit ${limit}`;
    return rows.map((r) => dec(r.data as Uint8Array));
  }

  async events(source: string, symbol: string, from: number, to: number, limit: number, asOf?: number): Promise<MarketEvent[]> {
    if (asOf !== undefined) {
      // state of every event as known at `asOf` (no look-ahead): latest status row not after asOf
      const rows = await this.sql`select distinct on (id) body from oft.event_status
        where source=${source} and symbol=${symbol} and status_at <= ${asOf} and (body->>'t')::bigint between ${from} and ${to}
        order by id, status_at desc limit ${limit}`;
      return rows.map((r) => r.body as MarketEvent).sort((a, b) => a.t - b.t);
    }
    const rows = await this.sql`select body from oft.events where source=${source} and symbol=${symbol} and t>=${from} and t<=${to} order by t desc limit ${limit}`;
    return rows.map((r) => r.body as MarketEvent).reverse();
  }

  async candles(source: string, symbol: string, from: number, to: number): Promise<(Candle & { origin: string })[]> {
    const rows = await this.sql`select t,o,h,l,c,v,bv,n,origin from oft.candles_1m where source=${source} and symbol=${symbol} and t>=${from} and t<=${to} order by t limit 20000`;
    return rows.map((r) => ({ t: n(r.t), o: n(r.o), h: n(r.h), l: n(r.l), c: n(r.c), v: n(r.v), bv: n(r.bv), n: r.n === null ? undefined : n(r.n), origin: String(r.origin) }));
  }

  async gaps(source: string, symbol: string, from: number, to: number): Promise<GapRow[]> {
    const rows = await this.sql`select * from oft.gaps where source=${source} and symbol=${symbol} and coalesce(t1, ${to}) >= ${from} and t0 <= ${to} order by t0 limit 1000`;
    return rows.map((r) => ({ source: String(r.source), symbol: String(r.symbol), stream: String(r.stream), t0: n(r.t0), t1: r.t1 === null ? null : n(r.t1), reason: String(r.reason) }));
  }

  async upsertKlines(source: string, symbol: string, tf: string, rows: Candle[]): Promise<void> {
    for (let i = 0; i < rows.length; i += 2000) {
      const vals = rows.slice(i, i + 2000).map((c) => ({ source, symbol, tf, t: c.t, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v }));
      await this.sql`insert into oft.klines ${this.sql(vals)} on conflict (source, symbol, tf, t) do update set o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v`;
    }
  }

  async klines(source: string, symbol: string, tf: string, from: number, to: number): Promise<Candle[]> {
    const out: Candle[] = [];
    // paged: a year of 15m bars is ~35k rows
    let cursor = from;
    for (;;) {
      const rows = await this.sql`select t,o,h,l,c,v from oft.klines where source=${source} and symbol=${symbol} and tf=${tf} and t>=${cursor} and t<=${to} order by t limit 10000`;
      for (const r of rows) out.push({ t: n(r.t), o: n(r.o), h: n(r.h), l: n(r.l), c: n(r.c), v: n(r.v), bv: 0 });
      if (rows.length < 10000) break;
      cursor = n(rows[rows.length - 1].t) + 1;
    }
    return out;
  }

  async putInstrument(meta: { source: string; symbol: string }): Promise<void> {
    await this.sql`insert into oft.instruments (source, symbol, meta) values (${meta.source}, ${meta.symbol}, ${this.sql.json(meta as never)})
      on conflict (source, symbol) do update set meta=excluded.meta, updated_at=now()`;
  }

  async getInstrument<T>(source: string, symbol: string): Promise<T | undefined> {
    const r = await this.sql`select meta from oft.instruments where source=${source} and symbol=${symbol}`;
    return r.length ? (r[0].meta as T) : undefined;
  }

  async listInstruments<T>(source: string): Promise<T[]> {
    const r = await this.sql`select meta from oft.instruments where source=${source} order by symbol`;
    return r.map((x) => x.meta as T);
  }

  async coverage(source: string, symbol: string): Promise<{ stream: string; t0: number; t1: number }[]> {
    const rows = await this.sql`select stream, t0, t1 from oft.coverage where source=${source} and symbol=${symbol} order by t0`;
    return rows.map((r) => ({ stream: String(r.stream), t0: n(r.t0), t1: n(r.t1) }));
  }

  async getSetting<T>(k: string): Promise<T | undefined> {
    const r = await this.sql`select v from oft.settings where k=${k}`;
    return r.length ? (r[0].v as T) : undefined;
  }
  async setSetting(k: string, v: unknown): Promise<void> {
    await this.sql`insert into oft.settings (k, v) values (${k}, ${this.sql.json(v as never)}) on conflict (k) do update set v=excluded.v, updated_at=now()`;
  }
  async recordList(): Promise<{ source: string; symbol: string; enabled: boolean }[]> {
    const r = await this.sql`select source, symbol, enabled from oft.record_list order by added_at`;
    return r.map((x) => ({ source: String(x.source), symbol: String(x.symbol), enabled: Boolean(x.enabled) }));
  }
  async setRecord(source: string, symbol: string, enabled: boolean): Promise<void> {
    await this.sql`insert into oft.record_list (source, symbol, enabled) values (${source}, ${symbol}, ${enabled}) on conflict (source, symbol) do update set enabled=excluded.enabled`;
  }
  async getPaper(account: string): Promise<unknown | undefined> {
    const r = await this.sql`select state from oft.paper where account=${account}`;
    return r.length ? r[0].state : undefined;
  }
  async setPaper(account: string, state: unknown): Promise<void> {
    await this.sql`insert into oft.paper (account, state) values (${account}, ${this.sql.json(state as never)}) on conflict (account) do update set state=excluded.state, updated_at=now()`;
  }
  async logAlert(row: { t: number; ruleId: number; source: string; symbol: string; eventId: string; body: unknown }): Promise<boolean> {
    const r = await this.sql`insert into oft.alert_log (t, rule_id, source, symbol, event_id, body) values (${row.t}, ${row.ruleId}, ${row.source}, ${row.symbol}, ${row.eventId}, ${this.sql.json(row.body as never)}) on conflict do nothing returning id`;
    return r.length > 0;
  }
  async alertLog(limit: number) {
    const r = await this.sql`select t, rule_id, source, symbol, event_id, body from oft.alert_log order by t desc limit ${limit}`;
    return r.map((x) => ({ t: n(x.t), rule_id: n(x.rule_id), source: String(x.source), symbol: String(x.symbol), event_id: String(x.event_id), body: x.body }));
  }

  async sizes(): Promise<{ dbBytes: number; tables: Record<string, number> }> {
    const db = await this.sql`select pg_database_size(current_database())::bigint as b`;
    const t = await this.sql`select c.relname as name, pg_total_relation_size(c.oid)::bigint as b from pg_class c join pg_namespace ns on ns.oid=c.relnamespace where ns.nspname='oft' and c.relkind='r'`;
    const tables: Record<string, number> = {};
    for (const r of t) tables[String(r.name)] = n(r.b);
    return { dbBytes: n(db[0].b), tables };
  }

  async purge(policy: { heat10Ms: number; heat60Ms: number; eventsMs: number; candlesMs: number }, now: number): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    out.heat10 = (await this.sql`delete from oft.heat_tiles where res_s=10 and t < ${now - policy.heat10Ms}`).count;
    out.heat60 = (await this.sql`delete from oft.heat_tiles where res_s=60 and t < ${now - policy.heat60Ms}`).count;
    out.events = (await this.sql`delete from oft.events where t < ${now - policy.eventsMs}`).count;
    out.eventStatus = (await this.sql`delete from oft.event_status where status_at < ${now - policy.eventsMs}`).count;
    out.candles = (await this.sql`delete from oft.candles_1m where t < ${now - policy.candlesMs}`).count;
    return out;
  }

  async deleteRange(source: string, symbol: string, from: number, to: number): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    out.heat = (await this.sql`delete from oft.heat_tiles where source=${source} and symbol=${symbol} and t between ${from} and ${to}`).count;
    out.events = (await this.sql`delete from oft.events where source=${source} and symbol=${symbol} and t between ${from} and ${to}`).count;
    out.eventStatus = (await this.sql`delete from oft.event_status where source=${source} and symbol=${symbol} and status_at between ${from} and ${to}`).count;
    out.candles = (await this.sql`delete from oft.candles_1m where source=${source} and symbol=${symbol} and t between ${from} and ${to}`).count;
    return out;
  }
}

function normManifest(r: Record<string, unknown>): ArchiveManifest {
  return {
    path: String(r.path),
    source: String(r.source),
    symbol: String(r.symbol),
    kind: String(r.kind),
    schema_version: n(r.schema_version),
    t0: n(r.t0),
    t1: n(r.t1),
    first_update_id: r.first_update_id === null ? null : n(r.first_update_id),
    last_update_id: r.last_update_id === null ? null : n(r.last_update_id),
    first_trade_id: r.first_trade_id === null ? null : n(r.first_trade_id),
    last_trade_id: r.last_trade_id === null ? null : n(r.last_trade_id),
    diffs: n(r.diffs),
    trades: n(r.trades),
    depth_band: n(r.depth_band),
    bytes: n(r.bytes),
    sha256: String(r.sha256),
    status: r.status === 'final' ? 'final' : 'pending',
  };
}
