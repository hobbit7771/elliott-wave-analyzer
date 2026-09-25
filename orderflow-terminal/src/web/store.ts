// Client-side state for the selected instrument. Everything here comes from the server
// (REST history + live WebSocket); nothing is generated locally.
import type { BookStats, FeedStatus, HeatColumn, InstrumentMeta, LargeOrder, LiquidityCluster, MarketEvent, SourceId, Trade, BookSide } from '../core/types.js';
import type { Timeframe } from '../core/candles.js';

export interface PersistStatusView {
  enabled: boolean;
  lastOkAt: number;
  lastError: string;
  lastErrorAt?: number;
  queued: number;
  oldestUnsavedMs: number;
  dropped: number;
  archive: { blocks: number; bytes: number; lastPath: string; lastAt: number; pending: number; failed: number };
}
export interface StatusX extends FeedStatus {
  gate?: boolean;
  gateReason?: string;
  persist?: PersistStatusView;
  streams?: Record<string, { route: string; connected: boolean; lastMsgAt: number; messages: number; reconnects: number; url: string }>;
  verify?: Record<string, Record<string, number | string>>;
  rest?: Record<string, { ok: number; fail: number; lastStatus: number; lastError: string; bannedUntil: number }>;
  heatStep?: number;
  cvd?: number;
  atr1m?: number;
}
export interface BookMsg {
  t: number;
  bids: [number, number][];
  asks: [number, number][];
  stats: BookStats | null;
  bbo: { bid: number; ask: number; t: number } | null;
}
export interface Deriv {
  mark?: number;
  index?: number;
  funding?: number;
  nextFunding?: number;
  oi?: number;
  oiT?: number;
}
export interface IceCandidate {
  side: BookSide;
  price: number;
  confidence: number;
  hidden: number;
  refills: number;
  eligible: boolean;
}
export interface Liq {
  t: number;
  side: 'buy' | 'sell';
  price: number;
  qty: number;
}

type Topic = 'meta' | 'status' | 'book' | 'trades' | 'heat' | 'events' | 'large' | 'clusters' | 'ice' | 'deriv' | 'net' | 'liq' | 'reset' | 'tf' | 'history';

const TRADE_CAP = 400_000;
const HEAT_CAP = 8000;
const EVENT_CAP = 4000;

export class Store {
  source: SourceId = 'binance-futures';
  symbol = 'BTCUSDT';
  meta: InstrumentMeta | null = null;
  tf: Timeframe = '1m';
  ticksPerBar = 100;
  net = { state: 'connecting', rtt: NaN, offset: 0, received: 0, droppedClient: 0 };
  status: StatusX | null = null;
  book: BookMsg | null = null;
  trades: Trade[] = [];
  /** earliest time for which trades are loaded contiguously */
  tradesFrom = 0;
  heat: HeatColumn[] = [];
  heatFrom = 0;
  events = new Map<string, MarketEvent>();
  large: { thr: { bid: number; ask: number; warm: boolean; samples: number; atrFactor: number } | null; list: LargeOrder[] } = { thr: null, list: [] };
  clusters: { list: LiquidityCluster[]; vacuums: { side: BookSide; lo: number; hi: number; t: number }[] } = { list: [], vacuums: [] };
  ice: IceCandidate[] = [];
  deriv: Deriv = {};
  liqs: Liq[] = [];
  lastTrade: Trade | null = null;
  /** total number of live trades appended since the instrument was selected (for incremental consumers) */
  tradeSeq = 0;
  /** exec volume per price since the DOM analysis window start: key = price */
  private subs = new Map<Topic, Set<() => void>>();

  get key(): string {
    return `${this.source}:${this.symbol}`;
  }

  on(t: Topic, fn: () => void): () => void {
    let s = this.subs.get(t);
    if (!s) this.subs.set(t, (s = new Set()));
    s.add(fn);
    return () => s!.delete(fn);
  }
  emit(t: Topic): void {
    for (const fn of this.subs.get(t) ?? []) {
      try {
        fn();
      } catch (e) {
        console.error(e);
      }
    }
  }

  /** server time estimate */
  now(): number {
    return Date.now() + (this.net.offset || 0);
  }

  reset(): void {
    this.meta = null;
    this.status = null;
    this.book = null;
    this.trades = [];
    this.tradesFrom = 0;
    this.heat = [];
    this.heatFrom = 0;
    this.events.clear();
    this.large = { thr: null, list: [] };
    this.clusters = { list: [], vacuums: [] };
    this.ice = [];
    this.deriv = {};
    this.liqs = [];
    this.lastTrade = null;
    this.tradeSeq = 0;
    this.emit('reset');
  }

  addTrades(list: Trade[]): void {
    if (!list.length) return;
    const last = this.trades[this.trades.length - 1];
    // drop overlap with what we already have: by exchange trade id when known (ids are monotonic),
    // otherwise by time (several trades can share a millisecond, so equal times are kept)
    const fresh = !last ? list : last.id !== undefined ? list.filter((t) => t.id === undefined || t.id > last.id!) : list.filter((t) => t.t >= last.t);
    for (const t of fresh) this.trades.push(t);
    this.tradeSeq += fresh.length;
    if (this.trades.length > TRADE_CAP) {
      this.trades.splice(0, this.trades.length - TRADE_CAP);
      this.tradesFrom = this.trades[0].t;
    }
    if (fresh.length) this.lastTrade = fresh[fresh.length - 1];
  }

  /** merge historic trades (older) in front of the live buffer */
  prependTrades(hist: Trade[], from: number): void {
    const first = this.trades[0];
    const older = !first ? hist : first.id !== undefined ? hist.filter((t) => t.id !== undefined && t.id < first.id!) : hist.filter((t) => t.t < first.t);
    this.trades = older.concat(this.trades);
    if (this.trades.length > TRADE_CAP) this.trades.splice(0, this.trades.length - TRADE_CAP);
    this.tradesFrom = this.trades.length ? Math.min(from, this.trades[0].t) : from;
    if (!this.lastTrade && this.trades.length) this.lastTrade = this.trades[this.trades.length - 1];
  }

  addHeat(c: HeatColumn): void {
    const last = this.heat[this.heat.length - 1];
    if (last && c.t <= last.t) return;
    this.heat.push(c);
    if (this.heat.length > HEAT_CAP) {
      this.heat.splice(0, this.heat.length - HEAT_CAP);
      this.heatFrom = this.heat[0].t;
    }
  }

  /** Merge history columns with what is already loaded (union by time; fills seams between REST and live). */
  setHeatHistory(cols: HeatColumn[], from: number): void {
    const byT = new Map<number, HeatColumn>();
    for (const c of cols) byT.set(c.t, c);
    for (const c of this.heat) byT.set(c.t, c);
    this.heat = [...byT.values()].sort((a, b) => a.t - b.t);
    if (this.heat.length > HEAT_CAP) this.heat.splice(0, this.heat.length - HEAT_CAP);
    this.heatFrom = this.heatFrom ? Math.min(this.heatFrom, from) : from;
  }

  upsertEvent(e: MarketEvent): void {
    this.events.set(e.id, e);
    if (this.events.size > EVENT_CAP) {
      const k = this.events.keys().next().value;
      if (k !== undefined) this.events.delete(k);
    }
  }

  eventList(): MarketEvent[] {
    return [...this.events.values()].sort((a, b) => a.t - b.t);
  }
}

export const store = new Store();
