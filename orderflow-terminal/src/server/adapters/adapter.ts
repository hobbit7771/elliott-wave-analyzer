// Market-data adapter contract. One adapter per data source; each adapter only exposes what its
// public, free API really provides (capabilities), so the UI never offers unavailable features.
import type { BookSnapshot, Candle, DepthDiff, InstrumentMeta, SourceId, Trade } from '../../core/types.js';
import type { Timeframe } from '../../core/candles.js';
import type { SyncMode } from '../../core/orderbook.js';

export interface Capabilities {
  depthDiff: boolean;
  trades: boolean;
  bookTicker: boolean;
  markPrice: boolean;
  funding: boolean;
  openInterest: boolean;
  liquidations: boolean;
  /** kline intervals served natively by REST (others are built from recorded trades) */
  nativeIntervals: Timeframe[];
  /** max levels of the REST depth snapshot we request */
  snapshotDepth: number;
}

export type NormalizedMsg =
  | { kind: 'diff'; d: DepthDiff; eventTime: number }
  | { kind: 'trade'; tr: Trade; eventTime: number }
  | { kind: 'bbo'; t: number; u?: number; bid: number; bidQty: number; ask: number; askQty: number; eventTime: number }
  | { kind: 'mark'; t: number; mark: number; index: number; funding: number; nextFunding: number; eventTime: number }
  | { kind: 'liq'; t: number; side: 'buy' | 'sell'; price: number; qty: number; eventTime: number }
  | { kind: 'ignore' };

export interface StreamRoute {
  route: 'depth' | 'flow' | 'all';
  url: string;
}

export interface MarketAdapter {
  id: SourceId;
  name: string;
  venue: string;
  syncMode: SyncMode;
  caps: Capabilities;
  limitations: string[];
  listInstruments(): Promise<InstrumentMeta[]>;
  fetchSnapshot(symbol: string): Promise<BookSnapshot>;
  fetchKlines(symbol: string, tf: Timeframe, limit: number, endTime?: number): Promise<Candle[]>;
  fetchAggTrades(symbol: string, startTime: number, endTime: number): Promise<Trade[]>;
  /** aggTrades by id (fromId, up to 1000) — used to fill id gaps in the live trade stream */
  fetchAggTradesFromId?(symbol: string, fromId: number, limit: number): Promise<Trade[]>;
  fetchOpenInterest?(symbol: string): Promise<{ t: number; oi: number }>;
  /**
   * WebSocket connections needed for one instrument. 'depth' carries order-book diffs (+ BBO),
   * 'flow' carries trades / mark price / liquidations, 'all' carries everything on one socket.
   */
  streamRoutes(symbol: string): StreamRoute[];
  parse(raw: string): NormalizedMsg[];
}

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function getJson<T>(url: string, timeoutMs = 10_000): Promise<T> {
  const ac = new AbortController();
  const to = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ac.signal, headers: { 'user-agent': 'orderflow-terminal/0.1' } });
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      throw new HttpError(r.status, `${r.status} ${url.split('?')[0]} ${body.slice(0, 200)}`);
    }
    return (await r.json()) as T;
  } finally {
    clearTimeout(to);
  }
}
