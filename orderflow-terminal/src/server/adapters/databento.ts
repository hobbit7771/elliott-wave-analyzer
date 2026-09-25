// Databento adapter (CME Globex GLBX.MDP3, Nasdaq TotalView XNAS.ITCH) — historical HTTP API.
// STATUS: code written and unit-tested on the documented JSON record layout only. NOT verified against
// real Databento data: it needs DATABENTO_API_KEY, and data usage is billed (the owner has not approved
// any paid usage). The server never calls it unless the key is configured.
//
// Docs: https://databento.com/docs/api-reference-historical/timeseries/timeseries-get-range
//       https://databento.com/docs/schemas-and-data-formats/mbp-10 , .../trades
// Live data (Raw API / TCP gateway) is a separate, also billed, product and is not implemented here.
import type { Level, Trade } from '../../core/types.js';

export const DATABENTO_DATASETS = {
  'GLBX.MDP3': { name: 'CME Globex (MDP 3.0)', symbols: ['NQ.c.0', 'ES.c.0', 'GC.c.0', 'CL.c.0'], stype: 'continuous' },
  'XNAS.ITCH': { name: 'Nasdaq TotalView-ITCH', symbols: ['AAPL', 'NVDA', 'MSFT', 'QQQ'], stype: 'raw_symbol' },
} as const;
export type DatabentoDataset = keyof typeof DATABENTO_DATASETS;
export type DatabentoSchema = 'mbp-10' | 'trades';

/** JSON-encoded record as returned with encoding=json, pretty_px=true, pretty_ts=true. */
export interface DbnJsonRecord {
  hd: { ts_event: string; rtype: number; publisher_id: number; instrument_id: number };
  action: string; // A add, C cancel, M modify, T trade, F fill, R clear
  side: string; // B bid, A ask, N none; for trades: side of the AGGRESSOR
  price: string | null;
  size: number;
  sequence?: number;
  ts_recv?: string;
  levels?: { bid_px: string | null; ask_px: string | null; bid_sz: number; ask_sz: number; bid_ct: number; ask_ct: number }[];
}

const tsMs = (s: string): number => {
  const n = Date.parse(s);
  if (!Number.isFinite(n)) throw new Error(`bad Databento timestamp: ${s}`);
  return n;
};

/** Trade record → Trade. Aggressor 'B' = buy, 'A' = sell; 'N' (no aggressor, e.g. auction) is dropped. */
export function dbnTrade(r: DbnJsonRecord): Trade | null {
  if (r.action !== 'T' || r.price === null) return null;
  if (r.side !== 'B' && r.side !== 'A') return null;
  return { t: tsMs(r.hd.ts_event), price: +r.price, qty: r.size, side: r.side === 'B' ? 1 : -1, id: r.sequence };
}

/** MBP-10 record → top-10 book state after the event (full replacement of the visible 10 levels). */
export function dbnMbp10(r: DbnJsonRecord): { t: number; bids: Level[]; asks: Level[]; sequence?: number } | null {
  if (!r.levels) return null;
  const bids: Level[] = [];
  const asks: Level[] = [];
  for (const l of r.levels) {
    if (l.bid_px !== null && l.bid_sz > 0) bids.push([+l.bid_px, l.bid_sz]);
    if (l.ask_px !== null && l.ask_sz > 0) asks.push([+l.ask_px, l.ask_sz]);
  }
  return { t: tsMs(r.hd.ts_event), bids, asks, sequence: r.sequence };
}

export interface DatabentoRangeQuery {
  key: string;
  dataset: DatabentoDataset;
  symbol: string;
  schema: DatabentoSchema;
  start: string; // ISO 8601
  end: string;
  limit?: number;
}

/** Streams NDJSON records of timeseries.get_range. Billed per byte by Databento — call only with the owner's approval. */
export async function* databentoRange(q: DatabentoRangeQuery, base = process.env.DATABENTO_HIST_URL ?? 'https://hist.databento.com'): AsyncGenerator<DbnJsonRecord> {
  const body = new URLSearchParams({
    dataset: q.dataset,
    symbols: q.symbol,
    schema: q.schema,
    start: q.start,
    end: q.end,
    stype_in: DATABENTO_DATASETS[q.dataset].stype,
    encoding: 'json',
    pretty_px: 'true',
    pretty_ts: 'true',
  });
  if (q.limit) body.set('limit', String(q.limit));
  const res = await fetch(`${base}/v0/timeseries.get_range`, {
    method: 'POST',
    headers: { authorization: 'Basic ' + Buffer.from(q.key + ':').toString('base64'), 'content-type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok || !res.body) throw new Error(`Databento ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const dec = new TextDecoder();
  let buf = '';
  for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
    buf += dec.decode(chunk, { stream: true });
    let i: number;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) yield JSON.parse(line) as DbnJsonRecord;
    }
  }
  if (buf.trim()) yield JSON.parse(buf) as DbnJsonRecord;
}

export function databentoConfigured(env = process.env): boolean {
  return !!env.DATABENTO_API_KEY;
}
