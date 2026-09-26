// Bybit v5 USDT perpetuals (category=linear): public order book (orderbook.200), trades, tickers
// (mark / index / funding / open interest) and liquidations. Free public API, no key.
// Docs: https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
//
// Order-book sync: the snapshot arrives on the WebSocket itself (type "snapshot"); every "delta" carries
// an update id u that must continue the previous one by +1. Any break -> the depth socket reconnects and a
// fresh snapshot is applied (never patched over). u = 1 means the exchange restarted the stream: it comes
// as a snapshot and replaces the book.
import type { BookSnapshot, Candle, InstrumentMeta, Level, Trade } from '../../core/types.js';
import type { Timeframe } from '../../core/candles.js';
import { getJson, type MarketAdapter, type NormalizedMsg, type StreamRoute } from './adapter.js';

const TF: Partial<Record<Timeframe, string>> = { '1m': '1', '3m': '3', '5m': '5', '15m': '15', '30m': '30', '1h': '60', '4h': '240', '1d': 'D' };

interface Wrap<T> {
  retCode: number;
  retMsg: string;
  result: T;
}

const decimals = (s: string) => (s.includes('.') ? s.replace(/0+$/, '').split('.')[1]?.length ?? 0 : 0);

export class BybitLinearAdapter implements MarketAdapter {
  id = 'bybit-linear' as const;
  name = 'Bybit USDT Perpetual';
  venue = 'Bybit';
  syncMode = 'spot' as const; // continuity rule: next update id = previous + 1
  snapshotViaStream = true;
  caps = {
    depthDiff: true,
    trades: true,
    bookTicker: false, // orderbook.1 has its own id sequence, so it cannot be checked against orderbook.200
    markPrice: true,
    funding: true,
    openInterest: true,
    liquidations: true,
    nativeIntervals: ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'] as Timeframe[],
    snapshotDepth: 200,
    klineOpenIsPrevClose: true, // verified live: REST O(t) = C(t-1), and H/L include it
  };
  limitations = [
    'Стакан Bybit в публичном потоке — 200 уровней на сторону (orderbook.200, пакеты по 100 мс); глубже биржа бесплатно не отдаёт.',
    'Показан суммарный объём на цене; скрытый объём (айсберги) только предполагается.',
    'Идентификаторы сделок Bybit — строки: непрерывность сделок по id не проверяется (у Binance проверяется).',
    'Догрузка сделок по REST ограничена последними 1000 сделками.',
    'Проверка bookTicker против стакана недоступна: у orderbook.1 своя последовательность обновлений.',
  ];
  private rest = process.env.BYBIT_REST ?? 'https://api.bybit.com';
  private ws = process.env.BYBIT_WS ?? 'wss://stream.bybit.com/v5/public/linear';
  private tick = new Map<string, Record<string, string>>();

  private async get<T>(path: string): Promise<T> {
    const r = await getJson<Wrap<T>>(this.rest + path, 15_000);
    if (r.retCode !== 0) throw new Error(`Bybit ${r.retCode} ${r.retMsg}`);
    return r.result;
  }

  async listInstruments(): Promise<InstrumentMeta[]> {
    const out: InstrumentMeta[] = [];
    let cursor = '';
    for (let page = 0; page < 10; page++) {
      const r = await this.get<{ list: { symbol: string; status: string; contractType: string; baseCoin: string; quoteCoin: string; priceFilter: { tickSize: string }; lotSizeFilter: { qtyStep: string } }[]; nextPageCursor?: string }>(
        `/v5/market/instruments-info?category=linear&limit=1000${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
      );
      for (const s of r.list)
        if (s.status === 'Trading' && s.contractType === 'LinearPerpetual' && s.quoteCoin === 'USDT')
          out.push({ source: this.id, symbol: s.symbol, base: s.baseCoin, quote: s.quoteCoin, tickSize: +s.priceFilter.tickSize, stepSize: +s.lotSizeFilter.qtyStep, pricePrecision: decimals(s.priceFilter.tickSize), qtyPrecision: decimals(s.lotSizeFilter.qtyStep) });
      if (!r.nextPageCursor) break;
      cursor = r.nextPageCursor;
    }
    return out;
  }

  async fetchSnapshot(symbol: string): Promise<BookSnapshot> {
    // not used for sync (the stream snapshot is), kept for diagnostics
    const r = await this.get<{ b: [string, string][]; a: [string, string][]; u: number; ts: number }>(`/v5/market/orderbook?category=linear&symbol=${symbol}&limit=200`);
    return { lastUpdateId: r.u, bids: r.b.map(([p, q]) => [+p, +q]), asks: r.a.map(([p, q]) => [+p, +q]), t: r.ts };
  }

  async fetchKlines(symbol: string, tf: Timeframe, limit: number, endTime?: number): Promise<Candle[]> {
    const iv = TF[tf];
    if (!iv) throw new Error(`interval ${tf} not served natively by ${this.id}`);
    const r = await this.get<{ list: string[][] }>(`/v5/market/kline?category=linear&symbol=${symbol}&interval=${iv}&limit=${Math.min(limit, 1000)}${endTime ? `&end=${endTime}` : ''}`);
    // Bybit returns newest first
    return r.list.map((k) => ({ t: +k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5], bv: 0 })).reverse();
  }

  async fetchAggTrades(symbol: string, startTime: number, endTime: number): Promise<Trade[]> {
    const r = await this.get<{ list: { time: string; price: string; size: string; side: string }[] }>(`/v5/market/recent-trade?category=linear&symbol=${symbol}&limit=1000`);
    return r.list
      .map((x) => ({ t: +x.time, price: +x.price, qty: +x.size, side: (x.side === 'Buy' ? 1 : -1) as 1 | -1 }))
      .filter((x) => x.t >= startTime && x.t <= endTime)
      .sort((a, b) => a.t - b.t);
  }

  async fetchOpenInterest(symbol: string): Promise<{ t: number; oi: number }> {
    const r = await this.get<{ list: { openInterest: string; timestamp: string }[] }>(`/v5/market/open-interest?category=linear&symbol=${symbol}&intervalTime=5min&limit=1`);
    return { t: +r.list[0].timestamp, oi: +r.list[0].openInterest };
  }

  async fetchPrices(): Promise<Record<string, number>> {
    const r = await this.get<{ list: { symbol: string; lastPrice: string }[] }>('/v5/market/tickers?category=linear');
    return Object.fromEntries(r.list.map((x) => [x.symbol, +x.lastPrice]));
  }

  streamRoutes(symbol: string): StreamRoute[] {
    const ping = { everyMs: 20_000, payload: JSON.stringify({ op: 'ping' }) };
    return [
      { route: 'depth', url: this.ws, subscribe: JSON.stringify({ op: 'subscribe', args: [`orderbook.200.${symbol}`] }), appPing: ping },
      { route: 'flow', url: this.ws, subscribe: JSON.stringify({ op: 'subscribe', args: [`publicTrade.${symbol}`, `tickers.${symbol}`, `allLiquidation.${symbol}`] }), appPing: ping },
    ];
  }

  parse(raw: string): NormalizedMsg[] {
    let m: { topic?: string; type?: string; ts?: number; data?: unknown };
    try {
      m = JSON.parse(raw);
    } catch {
      return [{ kind: 'ignore' }];
    }
    const topic = m.topic ?? '';
    const ts = m.ts ?? Date.now();
    if (topic.startsWith('orderbook.')) {
      const d = m.data as { b: [string, string][]; a: [string, string][]; u: number };
      const conv = (l: [string, string][]): Level[] => l.map(([p, q]) => [+p, +q]);
      if (m.type === 'snapshot') return [{ kind: 'snapshot', eventTime: ts, snap: { lastUpdateId: d.u, bids: conv(d.b), asks: conv(d.a), t: ts } }];
      return [{ kind: 'diff', eventTime: ts, d: { t: ts, firstId: d.u, lastId: d.u, bids: conv(d.b), asks: conv(d.a) } }];
    }
    if (topic.startsWith('publicTrade.')) {
      return (m.data as { T: number; S: string; v: string; p: string }[]).map((x) => ({ kind: 'trade' as const, eventTime: x.T, tr: { t: x.T, price: +x.p, qty: +x.v, side: (x.S === 'Buy' ? 1 : -1) as 1 | -1 } }));
    }
    if (topic.startsWith('tickers.')) {
      // deltas carry only the changed fields: merge into the last known ticker
      const d = m.data as Record<string, string>;
      const cur = { ...(this.tick.get(d.symbol) ?? {}), ...d };
      this.tick.set(d.symbol, cur);
      if (!cur.markPrice) return [{ kind: 'ignore' }];
      return [{ kind: 'mark', eventTime: ts, t: ts, mark: +cur.markPrice, index: +(cur.indexPrice ?? NaN), funding: +(cur.fundingRate ?? NaN), nextFunding: +(cur.nextFundingTime ?? 0) }];
    }
    if (topic.startsWith('allLiquidation.')) {
      // S is the side of the liquidated POSITION: a long (Buy) is closed by a sell order
      return (m.data as { T: number; S: string; v: string; p: string }[]).map((x) => ({ kind: 'liq' as const, eventTime: x.T, t: x.T, side: x.S === 'Buy' ? ('sell' as const) : ('buy' as const), price: +x.p, qty: +x.v }));
    }
    return [{ kind: 'ignore' }];
  }
}
