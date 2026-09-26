// Binance USD-M Futures and Binance Spot adapters (public, free endpoints only).
import type { BookSnapshot, Candle, InstrumentMeta, Level, SourceId, Trade } from '../../core/types.js';
import type { Timeframe } from '../../core/candles.js';
import { decimalsOf } from '../../core/precision.js';
import { type MarketAdapter, type NormalizedMsg, type StreamRoute, type Ticker24, getJson } from './adapter.js';

const TF_BINANCE: Partial<Record<Timeframe, string>> = {
  '1s': '1s',
  '1m': '1m',
  '3m': '3m',
  '5m': '5m',
  '15m': '15m',
  '30m': '30m',
  '1h': '1h',
  '4h': '4h',
  '1d': '1d',
};

type RawKline = [number, string, string, string, string, string, number, string, number, string, string, string];
interface RawAggTrade {
  a: number;
  p: string;
  q: string;
  T: number;
  m: boolean;
}
interface RawSymbol {
  symbol: string;
  status: string;
  baseAsset: string;
  quoteAsset: string;
  contractType?: string;
  underlyingType?: string;
  underlyingSubType?: string[];
  pricePrecision?: number;
  quantityPrecision?: number;
  baseAssetPrecision?: number;
  filters: { filterType: string; tickSize?: string; stepSize?: string }[];
}

// Assets whose "real" reference market is not a crypto exchange. Binance lists some of them as
// perpetuals; the order book shown is Binance's own book, never CME/COMEX/OTC liquidity.
const TRADFI_BASES = new Set(['XAU', 'XAG', 'XPT', 'XPD', 'PAXG', 'XAUT', 'BRENT', 'WTI', 'CL', 'NG', 'SPX', 'NDX', 'NQ', 'ES', 'DJI', 'GC']);

function tradfiNote(base: string, venue: string, sub?: string[]): string | undefined {
  const isTradfi = TRADFI_BASES.has(base) || (sub ?? []).some((s) => /commodit|index|stock|tradfi|metal/i.test(s));
  if (!isTradfi) return undefined;
  return `${venue} ${base} contract: this is ${venue}'s own order book, NOT the CME/COMEX futures book or OTC/CFD liquidity. Do not compare it with CME data.`;
}

abstract class BinanceBase implements MarketAdapter {
  abstract id: SourceId;
  abstract name: string;
  abstract venue: string;
  abstract syncMode: 'futures' | 'spot';
  abstract caps: MarketAdapter['caps'];
  abstract limitations: string[];
  protected abstract rest: string;
  protected abstract ws: string;
  protected abstract path: { depth: string; klines: string; aggTrades: string; exchangeInfo: string };
  private instCache: { t: number; list: InstrumentMeta[] } | null = null;

  async listInstruments(): Promise<InstrumentMeta[]> {
    if (this.instCache && Date.now() - this.instCache.t < 3_600_000) return this.instCache.list;
    const info = await getJson<{ symbols: RawSymbol[] }>(this.rest + this.path.exchangeInfo, 20_000);
    const list: InstrumentMeta[] = [];
    for (const s of info.symbols) {
      if (s.status !== 'TRADING') continue;
      if (!this.accept(s)) continue;
      const pf = s.filters.find((f) => f.filterType === 'PRICE_FILTER');
      const lf = s.filters.find((f) => f.filterType === 'LOT_SIZE');
      const tick = parseFloat(pf?.tickSize ?? '0');
      const step = parseFloat(lf?.stepSize ?? '0');
      if (!(tick > 0) || !(step > 0)) continue;
      list.push({
        source: this.id,
        symbol: s.symbol,
        base: s.baseAsset,
        quote: s.quoteAsset,
        tickSize: tick,
        stepSize: step,
        pricePrecision: decimalsOf(tick),
        qtyPrecision: decimalsOf(step),
        note: tradfiNote(s.baseAsset, this.venue, s.underlyingSubType),
      });
    }
    list.sort((a, b) => a.symbol.localeCompare(b.symbol));
    this.instCache = { t: Date.now(), list };
    return list;
  }

  protected abstract accept(s: RawSymbol): boolean;

  async fetchSnapshot(symbol: string): Promise<BookSnapshot> {
    const r = await getJson<{ lastUpdateId: number; E?: number; T?: number; bids: [string, string][]; asks: [string, string][] }>(
      `${this.rest}${this.path.depth}?symbol=${symbol}&limit=${this.caps.snapshotDepth}`,
      10_000,
      20_000, // the book cannot sync without it: probe a banned host every 20 s
    );
    const conv = (l: [string, string][]): Level[] => l.map(([p, q]) => [parseFloat(p), parseFloat(q)]);
    return { lastUpdateId: r.lastUpdateId, t: r.T ?? r.E ?? Date.now(), bids: conv(r.bids), asks: conv(r.asks) };
  }

  async fetchKlines(symbol: string, tf: Timeframe, limit: number, endTime?: number): Promise<Candle[]> {
    const iv = TF_BINANCE[tf];
    if (!iv || !this.caps.nativeIntervals.includes(tf)) throw new Error(`interval ${tf} not served natively by ${this.id}`);
    const lim = Math.min(limit, this.syncMode === 'futures' ? 1500 : 1000);
    const q = `${this.rest}${this.path.klines}?symbol=${symbol}&interval=${iv}&limit=${lim}${endTime ? `&endTime=${endTime}` : ''}`;
    const rows = await getJson<RawKline[]>(q);
    return rows.map((k) => ({ t: k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5], bv: +k[9], n: k[8] }));
  }

  async fetchAggTrades(symbol: string, startTime: number, endTime: number): Promise<Trade[]> {
    const rows = await getJson<RawAggTrade[]>(`${this.rest}${this.path.aggTrades}?symbol=${symbol}&startTime=${startTime}&endTime=${endTime}&limit=1000`);
    return rows.map((r) => ({ t: r.T, price: +r.p, qty: +r.q, side: r.m ? -1 : 1, id: r.a }));
  }

  /** Last traded price of every symbol in one request (weight 2) — used by the level watchlist. */
  async fetchPrices(): Promise<Record<string, number>> {
    const rows = await getJson<{ symbol: string; price: string }[]>(`${this.rest}${this.path.klines.replace('klines', 'ticker/price')}`);
    return Object.fromEntries(rows.map((r) => [r.symbol, +r.price]));
  }

  /** 24h stats of every symbol (coin picker); weight 40, so it is cached by the caller. */
  async fetchTickers(): Promise<Ticker24[]> {
    const rows = await getJson<{ symbol: string; lastPrice: string; priceChangePercent: string; quoteVolume: string }[]>(`${this.rest}${this.path.klines.replace('klines', 'ticker/24hr')}`);
    return rows.map((r) => ({ symbol: r.symbol, last: +r.lastPrice, change: +r.priceChangePercent / 100, turnover: +r.quoteVolume }));
  }

  abstract streamRoutes(symbol: string): StreamRoute[];

  async fetchAggTradesFromId(symbol: string, fromId: number, limit: number): Promise<Trade[]> {
    const rows = await getJson<RawAggTrade[]>(`${this.rest}${this.path.aggTrades}?symbol=${symbol}&fromId=${fromId}&limit=${Math.min(1000, limit)}`);
    return rows.map((r) => ({ t: r.T, price: +r.p, qty: +r.q, side: r.m ? -1 : 1, id: r.a }));
  }

  parse(raw: string): NormalizedMsg[] {
    let m: { stream?: string; data?: Record<string, unknown> };
    try {
      m = JSON.parse(raw);
    } catch {
      return [{ kind: 'ignore' }];
    }
    const d = (m.data ?? m) as Record<string, unknown> & { e?: string };
    switch (d.e) {
      case 'depthUpdate': {
        const conv = (l: [string, string][]): Level[] => l.map(([p, q]) => [parseFloat(p), parseFloat(q)]);
        const t = (d.T as number) ?? (d.E as number);
        return [
          {
            kind: 'diff',
            eventTime: d.E as number,
            d: { t, firstId: d.U as number, lastId: d.u as number, prevLastId: d.pu as number | undefined, bids: conv(d.b as [string, string][]), asks: conv(d.a as [string, string][]) },
          },
        ];
      }
      case 'aggTrade':
        return [{ kind: 'trade', eventTime: d.E as number, tr: { t: d.T as number, price: +(d.p as string), qty: +(d.q as string), side: d.m ? -1 : 1, id: d.a as number } }];
      case 'bookTicker':
        return [{ kind: 'bbo', u: d.u as number, eventTime: (d.E as number) ?? Date.now(), t: (d.T as number) ?? (d.E as number) ?? Date.now(), bid: +(d.b as string), bidQty: +(d.B as string), ask: +(d.a as string), askQty: +(d.A as string) }];
      case 'markPriceUpdate':
        return [{ kind: 'mark', eventTime: d.E as number, t: d.E as number, mark: +(d.p as string), index: +(d.i as string), funding: +(d.r as string), nextFunding: d.T as number }];
      case 'forceOrder': {
        const o = d.o as Record<string, string | number>;
        return [{ kind: 'liq', eventTime: d.E as number, t: o.T as number, side: o.S === 'BUY' ? 'buy' : 'sell', price: +(o.ap || o.p), qty: +(o.z || o.q) }];
      }
      default:
        // spot bookTicker has no "e" field
        if (d.u !== undefined && d.b !== undefined && d.a !== undefined && d.B !== undefined) {
          return [{ kind: 'bbo', u: d.u as number, eventTime: Date.now(), t: Date.now(), bid: +(d.b as string), bidQty: +(d.B as string), ask: +(d.a as string), askQty: +(d.A as string) }];
        }
        return [{ kind: 'ignore' }];
    }
  }
}

export class BinanceFuturesAdapter extends BinanceBase {
  id = 'binance-futures' as const;
  name = 'Binance USDⓈ-M Futures';
  venue = 'Binance Futures';
  syncMode = 'futures' as const;
  caps = {
    depthDiff: true,
    trades: true,
    bookTicker: true,
    markPrice: true,
    funding: true,
    openInterest: true,
    liquidations: true,
    nativeIntervals: ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'] as Timeframe[],
    snapshotDepth: 1000,
  };
  limitations = [
    'Public depth shows displayed size only; hidden/iceberg size is inferred (Probable Iceberg), never observed.',
    'Diff depth is batched every 100 ms: individual order add/cancel events inside a batch are not visible.',
    'Liquidation stream (forceOrder) sends at most one liquidation per symbol per 1000 ms (Binance snapshot rule).',
    'Open interest has no public WebSocket; it is polled via REST every 15 s.',
    'No 1s klines on futures REST: 1s and tick charts are built from recorded / backfilled aggTrades.',
    'Order-book history (heatmap) exists only from the moment the server started recording.',
    'Futures API is geo-restricted in some regions (e.g. US IPs get HTTP 451); deploy in an allowed region.',
  ];
  protected rest = process.env.BINANCE_FUTURES_REST ?? 'https://fapi.binance.com';
  // Since the 2026 base-URL split: /public = high-frequency book data, /market = trades, mark price, liquidations.
  protected ws = process.env.BINANCE_FUTURES_WS_BASE ?? 'wss://fstream.binance.com';
  protected path = { depth: '/fapi/v1/depth', klines: '/fapi/v1/klines', aggTrades: '/fapi/v1/aggTrades', exchangeInfo: '/fapi/v1/exchangeInfo' };

  protected accept(s: RawSymbol): boolean {
    return s.contractType === 'PERPETUAL' && (s.quoteAsset === 'USDT' || s.quoteAsset === 'USDC');
  }

  streamRoutes(symbol: string): StreamRoute[] {
    const s = symbol.toLowerCase();
    return [
      { route: 'depth', url: `${this.ws}/public/stream?streams=${s}@depth@100ms/${s}@bookTicker` },
      { route: 'flow', url: `${this.ws}/market/stream?streams=${s}@aggTrade/${s}@markPrice@1s/${s}@forceOrder` },
    ];
  }

  async fetchOpenInterest(symbol: string): Promise<{ t: number; oi: number }> {
    const r = await getJson<{ openInterest: string; time: number }>(`${this.rest}/fapi/v1/openInterest?symbol=${symbol}`);
    return { t: r.time, oi: +r.openInterest };
  }
}

export class BinanceSpotAdapter extends BinanceBase {
  id = 'binance-spot' as const;
  name = 'Binance Spot';
  venue = 'Binance Spot';
  syncMode = 'spot' as const;
  caps = {
    depthDiff: true,
    trades: true,
    bookTicker: true,
    markPrice: false,
    funding: false,
    openInterest: false,
    liquidations: false,
    nativeIntervals: ['1s', '1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'] as Timeframe[],
    snapshotDepth: 1000,
  };
  limitations = [
    'Spot has no mark price, funding, open interest or liquidations.',
    'Public depth shows displayed size only; hidden/iceberg size is inferred, never observed.',
    'Diff depth is batched every 100 ms.',
    'Order-book history (heatmap) exists only from the moment the server started recording.',
  ];
  protected rest = process.env.BINANCE_SPOT_REST ?? 'https://api.binance.com';
  protected ws = process.env.BINANCE_SPOT_WS ?? 'wss://stream.binance.com:9443/stream';
  protected path = { depth: '/api/v3/depth', klines: '/api/v3/klines', aggTrades: '/api/v3/aggTrades', exchangeInfo: '/api/v3/exchangeInfo?permissions=SPOT' };

  streamRoutes(symbol: string): StreamRoute[] {
    const s = symbol.toLowerCase();
    return [{ route: 'all', url: `${this.ws}?streams=${s}@depth@100ms/${s}@aggTrade/${s}@bookTicker` }];
  }

  protected accept(s: RawSymbol): boolean {
    return s.quoteAsset === 'USDT' || s.quoteAsset === 'USDC' || s.quoteAsset === 'FDUSD';
  }
}
