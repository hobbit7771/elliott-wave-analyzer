// Historical liquidity recorder on SQLite (node:sqlite, no native deps).
// Retention tiers keep storage bounded:
//   heat 1s columns  -> HEAT_RAW_RETENTION   (default 2 h)
//   heat 10s columns -> HEAT_10S_RETENTION   (default 24 h)
//   heat 60s columns -> HEAT_60S_RETENTION   (default 7 d)
//   trades           -> TRADES_RETENTION     (default 6 h)
//   events / 1m candles -> 7 d
import { DatabaseSync } from 'node:sqlite';
import { deflateSync, inflateSync } from 'node:zlib';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type { Candle, HeatColumn, MarketEvent, Trade } from '../core/types.js';
import { downsample } from '../core/heatmap.js';

export interface Retention {
  heatRawMs: number;
  heat10Ms: number;
  heat60Ms: number;
  tradesMs: number;
  eventsMs: number;
}

export const DEFAULT_RETENTION: Retention = {
  heatRawMs: +(process.env.HEAT_RAW_RETENTION_MS ?? 45 * 60_000),
  heat10Ms: +(process.env.HEAT_10S_RETENTION_MS ?? 6 * 3600_000),
  heat60Ms: +(process.env.HEAT_60S_RETENTION_MS ?? 2 * 24 * 3600_000),
  tradesMs: +(process.env.TRADES_RETENTION_MS ?? 2 * 3600_000),
  eventsMs: 7 * 24 * 3600_000,
};

export function openDb(path: string): DatabaseSync {
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);
  db.exec(`
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS trades (sym TEXT NOT NULL, t INTEGER NOT NULL, p REAL NOT NULL, q REAL NOT NULL, s INTEGER NOT NULL, id INTEGER);
    CREATE INDEX IF NOT EXISTS trades_sym_t ON trades(sym, t);
    CREATE UNIQUE INDEX IF NOT EXISTS trades_sym_id ON trades(sym, id) WHERE id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS heat (sym TEXT NOT NULL, res INTEGER NOT NULL, t INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY (sym, res, t)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS events (sym TEXT NOT NULL, id TEXT NOT NULL, t INTEGER NOT NULL, kind TEXT NOT NULL, conf INTEGER NOT NULL, json TEXT NOT NULL, PRIMARY KEY (sym, id));
    CREATE INDEX IF NOT EXISTS events_sym_t ON events(sym, t);
    CREATE TABLE IF NOT EXISTS candles (sym TEXT NOT NULL, tf TEXT NOT NULL, t INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL, bv REAL, n INTEGER, PRIMARY KEY (sym, tf, t)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
  `);
  return db;
}

export const symKey = (source: string, symbol: string): string => `${source}:${symbol}`;

const enc = (c: HeatColumn): Uint8Array => deflateSync(Buffer.from(JSON.stringify(c)));
const dec = (b: Uint8Array): HeatColumn => JSON.parse(inflateSync(b).toString());

/** Write side (lives in the ingestion worker). Buffers rows and flushes in one transaction. */
export class Recorder {
  private tradesBuf: Trade[] = [];
  private heatBuf: HeatColumn[] = [];
  private eventBuf: MarketEvent[] = [];
  private candleBuf: Candle[] = [];
  private lastAgg10 = 0;
  private lastAgg60 = 0;
  private lastPurge = 0;
  rows = { trades: 0, heat: 0, events: 0 };

  constructor(private db: DatabaseSync, private key: string, private ret: Retention = DEFAULT_RETENTION) {}

  private recent: Trade[] = [];
  /** the last few thousand live trades (for REST cross-checks) */
  recentTrades(): Trade[] {
    return this.recent;
  }

  trade(t: Trade): void {
    this.recent.push(t);
    if (this.recent.length > 6000) this.recent.splice(0, 1000);
    this.tradesBuf.push(t);
    if (this.tradesBuf.length > 50_000) this.flush(Date.now());
  }
  heat(c: HeatColumn): void {
    this.heatBuf.push(c);
  }
  event(e: MarketEvent): void {
    this.eventBuf.push(e);
  }
  candle1m(c: Candle): void {
    this.candleBuf.push(c);
  }

  flush(now: number): void {
    if (!this.tradesBuf.length && !this.heatBuf.length && !this.eventBuf.length && !this.candleBuf.length && now - this.lastPurge < 60_000) return;
    const db = this.db;
    db.exec('BEGIN');
    try {
      if (this.tradesBuf.length) {
        const st = db.prepare('INSERT OR IGNORE INTO trades (sym,t,p,q,s,id) VALUES (?,?,?,?,?,?)');
        for (const t of this.tradesBuf) st.run(this.key, t.t, t.price, t.qty, t.side, t.id ?? null);
        this.rows.trades += this.tradesBuf.length;
        this.tradesBuf = [];
      }
      if (this.heatBuf.length) {
        const st = db.prepare('INSERT OR REPLACE INTO heat (sym,res,t,data) VALUES (?,?,?,?)');
        for (const c of this.heatBuf) st.run(this.key, 1, c.t, enc(c));
        this.rows.heat += this.heatBuf.length;
        this.heatBuf = [];
      }
      if (this.eventBuf.length) {
        const st = db.prepare('INSERT OR REPLACE INTO events (sym,id,t,kind,conf,json) VALUES (?,?,?,?,?,?)');
        for (const e of this.eventBuf) st.run(this.key, e.id, e.t, e.kind, e.confidence, JSON.stringify(e));
        this.rows.events += this.eventBuf.length;
        this.eventBuf = [];
      }
      if (this.candleBuf.length) {
        const st = db.prepare('INSERT OR REPLACE INTO candles (sym,tf,t,o,h,l,c,v,bv,n) VALUES (?,?,?,?,?,?,?,?,?,?)');
        for (const c of this.candleBuf) st.run(this.key, '1m', c.t, c.o, c.h, c.l, c.c, c.v, c.bv, c.n ?? 0);
        this.candleBuf = [];
      }
      this.aggregate(now);
      if (now - this.lastPurge >= 60_000) {
        this.lastPurge = now;
        this.purge(now);
      }
      db.exec('COMMIT');
    } catch (e) {
      db.exec('ROLLBACK');
      throw e;
    }
  }

  /** Roll 1s columns into 10s, and 10s into 60s, for completed windows. */
  private aggregate(now: number): void {
    const roll = (fromRes: number, toRes: number, last: number): number => {
      const ms = toRes * 1000;
      const end = Math.floor(now / ms) * ms; // only complete windows
      const start = last || end - 10 * ms;
      if (end <= start) return last;
      const rows = this.db.prepare('SELECT data FROM heat WHERE sym=? AND res=? AND t>? AND t<=? ORDER BY t').all(this.key, fromRes, start, end) as { data: Uint8Array }[];
      if (rows.length) {
        const cols = downsample(rows.map((r) => dec(r.data)), ms);
        const st = this.db.prepare('INSERT OR REPLACE INTO heat (sym,res,t,data) VALUES (?,?,?,?)');
        for (const c of cols) st.run(this.key, toRes, c.t, enc(c));
      }
      return end;
    };
    if (now - this.lastAgg10 >= 10_000) this.lastAgg10 = roll(1, 10, this.lastAgg10);
    if (now - this.lastAgg60 >= 60_000) this.lastAgg60 = roll(10, 60, this.lastAgg60);
  }

  purge(now: number): void {
    const r = this.ret;
    this.db.prepare('DELETE FROM heat WHERE sym=? AND res=1 AND t<?').run(this.key, now - r.heatRawMs);
    this.db.prepare('DELETE FROM heat WHERE sym=? AND res=10 AND t<?').run(this.key, now - r.heat10Ms);
    this.db.prepare('DELETE FROM heat WHERE sym=? AND res=60 AND t<?').run(this.key, now - r.heat60Ms);
    this.db.prepare('DELETE FROM trades WHERE sym=? AND t<?').run(this.key, now - r.tradesMs);
    this.db.prepare('DELETE FROM events WHERE sym=? AND t<?').run(this.key, now - r.eventsMs);
    this.db.prepare("DELETE FROM candles WHERE sym=? AND tf='1m' AND t<?").run(this.key, now - r.eventsMs);
  }
}

/** Read side (main thread). */
export class HistoryReader {
  constructor(private db: DatabaseSync) {}

  trades(key: string, from: number, to: number, limit = 200_000): Trade[] {
    const rows = this.db
      .prepare('SELECT t,p,q,s,id FROM trades WHERE sym=? AND t>=? AND t<=? ORDER BY t DESC LIMIT ?')
      .all(key, from, to, limit) as { t: number; p: number; q: number; s: number; id: number | null }[];
    rows.reverse();
    return rows.map((r) => ({ t: r.t, price: r.p, qty: r.q, side: r.s === 1 ? 1 : -1, id: r.id ?? undefined }));
  }

  tradeRange(key: string): { from: number; to: number; count: number } {
    const r = this.db.prepare('SELECT MIN(t) a, MAX(t) b, COUNT(*) n FROM trades WHERE sym=?').get(key) as { a: number | null; b: number | null; n: number };
    return { from: r.a ?? 0, to: r.b ?? 0, count: r.n };
  }

  /** Pick the finest tier covering the range, then downsample to at most maxCols. */
  heat(key: string, from: number, to: number, maxCols = 1500): { res: number; cols: HeatColumn[] } {
    const span = Math.max(1, to - from);
    const oldest = (res: number): number => {
      const r = this.db.prepare('SELECT MIN(t) m FROM heat WHERE sym=? AND res=?').get(key, res) as { m: number | null };
      return r.m ?? Infinity;
    };
    // finest tier that (a) is not far finer than needed for the span and (b) reaches back as far as
    // any coarser tier does (or covers the range start). Fresh recordings therefore use 1 s columns.
    const tiers = [1, 10, 60];
    const old = tiers.map(oldest);
    let res = 60;
    for (let i = 0; i < tiers.length; i++) {
      const t = tiers[i];
      if (old[i] === Infinity) continue;
      if (span / (t * 1000) > maxCols * 3 && i < tiers.length - 1) continue;
      const coarserOldest = Math.min(Infinity, ...old.slice(i + 1));
      if (old[i] <= from + t * 1000 || old[i] <= coarserOldest + t * 1000 || i === tiers.length - 1) {
        res = t;
        break;
      }
    }
    // decoded columns are large (full book band): never decode more than ~maxCols of them; longer ranges
    // are thinned evenly in SQL before decoding
    const load = (r: number, a: number, b: number): HeatColumn[] => {
      const n = (this.db.prepare('SELECT COUNT(*) n FROM heat WHERE sym=? AND res=? AND t>? AND t<=?').get(key, r, a, b) as { n: number }).n;
      if (!n) return [];
      const stride = Math.max(1, Math.ceil(n / maxCols));
      const rows =
        stride === 1
          ? this.db.prepare('SELECT data FROM heat WHERE sym=? AND res=? AND t>? AND t<=? ORDER BY t').all(key, r, a, b)
          : this.db
              .prepare('SELECT data FROM (SELECT data, t, ROW_NUMBER() OVER (ORDER BY t) rn FROM heat WHERE sym=? AND res=? AND t>? AND t<=?) WHERE (rn - 1) % ? = 0 ORDER BY t')
              .all(key, r, a, b, stride);
      return (rows as { data: Uint8Array }[]).map((x) => dec(x.data));
    };
    let cols = load(res, from - 1, to);
    // the coarser tiers lag behind real time: fill the recent tail from finer tiers
    for (const finer of [10, 1].filter((f) => f < res)) {
      const lastT = cols.length ? cols[cols.length - 1].t : from - 1;
      const tail = load(finer, lastT, to);
      if (tail.length) cols = cols.concat(downsample(tail, res * 1000));
    }
    if (cols.length > maxCols) {
      const bucket = Math.ceil(span / maxCols / 1000) * 1000;
      cols = downsample(cols, Math.max(bucket, res * 1000));
    }
    return { res, cols };
  }

  heatRange(key: string): { from: number; to: number } {
    const r = this.db.prepare('SELECT MIN(t) a, MAX(t) b FROM heat WHERE sym=?').get(key) as { a: number | null; b: number | null };
    return { from: r.a ?? 0, to: r.b ?? 0 };
  }

  events(key: string, from: number, to: number, limit = 5000): MarketEvent[] {
    const rows = this.db.prepare('SELECT json FROM events WHERE sym=? AND t>=? AND t<=? ORDER BY t DESC LIMIT ?').all(key, from, to, limit) as { json: string }[];
    return rows.reverse().map((r) => JSON.parse(r.json));
  }

  candles1m(key: string, from: number, to: number): Candle[] {
    return (this.db.prepare("SELECT t,o,h,l,c,v,bv,n FROM candles WHERE sym=? AND tf='1m' AND t>=? AND t<=? ORDER BY t").all(key, from, to) as unknown as Candle[]).map((c) => ({ ...c }));
  }

  getSetting<T>(k: string): T | undefined {
    const r = this.db.prepare('SELECT v FROM settings WHERE k=?').get(k) as { v: string } | undefined;
    return r ? (JSON.parse(r.v) as T) : undefined;
  }
  setSetting(k: string, v: unknown): void {
    this.db.prepare('INSERT OR REPLACE INTO settings (k,v) VALUES (?,?)').run(k, JSON.stringify(v));
  }

  clear(key: string): void {
    for (const t of ['trades', 'heat', 'events', 'candles']) this.db.prepare(`DELETE FROM ${t} WHERE sym=?`).run(key);
  }

  sizeInfo(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const t of ['trades', 'heat', 'events', 'candles']) out[t] = (this.db.prepare(`SELECT COUNT(*) n FROM ${t}`).get() as { n: number }).n;
    return out;
  }
}
