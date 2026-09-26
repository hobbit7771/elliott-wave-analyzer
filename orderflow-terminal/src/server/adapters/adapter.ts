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
  /** REST klines open at the previous bar's close (Bybit), not at the bar's first trade */
  klineOpenIsPrevClose?: boolean;
}

export type NormalizedMsg =
  | { kind: 'diff'; d: DepthDiff; eventTime: number }
  | { kind: 'trade'; tr: Trade; eventTime: number }
  | { kind: 'bbo'; t: number; u?: number; bid: number; bidQty: number; ask: number; askQty: number; eventTime: number }
  | { kind: 'mark'; t: number; mark: number; index: number; funding: number; nextFunding: number; eventTime: number }
  | { kind: 'snapshot'; snap: BookSnapshot; eventTime: number }
  | { kind: 'liq'; t: number; side: 'buy' | 'sell'; price: number; qty: number; eventTime: number }
  | { kind: 'ignore' };

export interface StreamRoute {
  route: 'depth' | 'flow' | 'all';
  url: string;
  /** subscription message sent on open (venues that subscribe after connecting) */
  subscribe?: string;
  appPing?: { everyMs: number; payload: string };
}

export interface MarketAdapter {
  id: SourceId;
  name: string;
  venue: string;
  syncMode: SyncMode;
  /** the order-book snapshot arrives on the depth stream itself (no REST snapshot); a gap forces a reconnect */
  snapshotViaStream?: boolean;
  caps: Capabilities;
  limitations: string[];
  listInstruments(): Promise<InstrumentMeta[]>;
  fetchSnapshot(symbol: string): Promise<BookSnapshot>;
  fetchKlines(symbol: string, tf: Timeframe, limit: number, endTime?: number): Promise<Candle[]>;
  fetchAggTrades(symbol: string, startTime: number, endTime: number): Promise<Trade[]>;
  /** aggTrades by id (fromId, up to 1000) — used to fill id gaps in the live trade stream */
  fetchAggTradesFromId?(symbol: string, fromId: number, limit: number): Promise<Trade[]>;
  fetchOpenInterest?(symbol: string): Promise<{ t: number; oi: number }>;
  fetchPrices?(): Promise<Record<string, number>>;
  /** 24h stats for every instrument (for the coin picker): last price, 24h change (fraction), 24h quote turnover */
  fetchTickers?(): Promise<Ticker24[]>;
  /**
   * WebSocket connections needed for one instrument. 'depth' carries order-book diffs (+ BBO),
   * 'flow' carries trades / mark price / liquidations, 'all' carries everything on one socket.
   */
  streamRoutes(symbol: string): StreamRoute[];
  parse(raw: string): NormalizedMsg[];
}

export interface Ticker24 {
  symbol: string;
  last: number;
  change: number;
  turnover: number;
}

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** REST health per host (exchanges ban shared cloud IPs; this is shown in diagnostics, never hidden). */
export const restHealth: Record<string, { ok: number; fail: number; lastStatus: number; lastError: string; bannedUntil: number; lastOkAt: number }> = {};

const lastProbe: Record<string, number> = {};

/**
 * `probeMs`: while the host reports a ban, send at most one request per this interval (default 5 min).
 * Binance extends an IP ban for requests made during it, so a banned host is left alone — but Render has more
 * than one outbound address (requests kept succeeding during earlier bans), so rare probes still get through;
 * the order-book snapshot, without which nothing syncs, probes more often.
 */
export async function getJson<T>(url: string, timeoutMs = 10_000, probeMs = 300_000): Promise<T> {
  const host = new URL(url).host;
  const h = (restHealth[host] ??= { ok: 0, fail: 0, lastStatus: 0, lastError: '', bannedUntil: 0, lastOkAt: 0 });
  if (Date.now() < h.bannedUntil) {
    if (Date.now() - (lastProbe[host] ?? 0) < probeMs) throw new HttpError(418, `${host} REST paused until ${new Date(h.bannedUntil).toISOString()} (IP ban / rate limit); request not sent`);
    lastProbe[host] = Date.now();
  }
  const ac = new AbortController();
  const to = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ac.signal, headers: { 'user-agent': 'orderflow-terminal/0.2' } });
    h.lastStatus = r.status;
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      h.fail++;
      h.lastError = `${r.status} ${body.slice(0, 160)}`;
      // Binance 418/429: IP ban / rate limit. "banned until <epoch ms>" is reported to the UI.
      const m = /banned until (\d{13})/.exec(body);
      if (m || r.status === 429 || r.status === 418) lastProbe[host] = Date.now(); // the next probe waits a full interval
      if (m) h.bannedUntil = +m[1];
      else if (r.status === 429 || r.status === 418) {
        const ra = Number(r.headers.get('retry-after'));
        h.bannedUntil = Date.now() + (Number.isFinite(ra) && ra > 0 ? ra * 1000 : 60_000);
      }
      throw new HttpError(r.status, `${r.status} ${url.split('?')[0]} ${body.slice(0, 200)}`);
    }
    h.ok++;
    h.lastOkAt = Date.now();
    h.bannedUntil = 0; // this request got through (another outbound address, or the ban was lifted)
    return (await r.json()) as T;
  } catch (e) {
    if (!(e instanceof HttpError)) {
      h.fail++;
      h.lastError = (e as Error).message;
    }
    throw e;
  } finally {
    clearTimeout(to);
  }
}
