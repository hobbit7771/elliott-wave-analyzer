// Normalized market-data model shared by server, client and tests.
// All timestamps are UTC epoch milliseconds.

export type SourceId = 'binance-futures' | 'binance-spot';

/** Aggressor side of a trade: +1 = buyer lifted the ask, -1 = seller hit the bid. */
export type AggSide = 1 | -1;
export type BookSide = 'bid' | 'ask';

export interface Trade {
  t: number;          // exchange trade time
  price: number;
  qty: number;
  side: AggSide;      // aggressor
  id?: number;        // exchange trade id (aggTrade a)
}

/** One side level update. qty = 0 means level removed. */
export type Level = [price: number, qty: number];

export interface BookSnapshot {
  lastUpdateId: number;
  t: number;
  bids: Level[];
  asks: Level[];
}

/** Normalized depth diff with sequence information. */
export interface DepthDiff {
  t: number;            // event/transaction time
  firstId: number;      // U
  lastId: number;       // u
  prevLastId?: number;  // pu (Binance futures only)
  bids: Level[];
  asks: Level[];
}

export interface Candle {
  t: number;       // open time
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;       // base volume
  bv: number;      // aggressive buy volume (taker buy)
  n?: number;      // trade count
}

export interface InstrumentMeta {
  source: SourceId;
  symbol: string;
  base: string;
  quote: string;
  tickSize: number;
  stepSize: number;
  pricePrecision: number;
  qtyPrecision: number;
  /** Warning shown in UI (e.g. commodity perps are NOT CME/COMEX books). */
  note?: string;
}

export interface BookStats {
  t: number;
  bestBid: number;
  bestAsk: number;
  bidQty: number;
  askQty: number;
  spread: number;
  mid: number;
  microprice: number;
  /** order-book imbalance over top N levels, in [-1, 1] (positive = bid heavy) */
  obi: number;
  /** cumulative order-flow imbalance (Cont-Kukanov-Stoikov) over the rolling window */
  ofi: number;
  depthLevels: number;
}

export type ConnState = 'connecting' | 'syncing' | 'connected' | 'reconnecting' | 'stale' | 'gap' | 'disconnected';

export interface FeedStatus {
  source: SourceId;
  symbol: string;
  state: ConnState;
  lastUpdate: number;       // wall clock of last message (ms)
  lastEventTime: number;    // exchange time of last message
  latencyMs: number;        // wall - exchange time, EWMA
  trades: number;           // trades received since start
  depthUpdates: number;
  depthLevels: number;
  gaps: number;
  resyncs: number;
  reconnects: number;
  dropped: number;          // messages dropped (buffer overflow / out of order / stale diffs)
  synced: boolean;
  message?: string;
}

export type EventKind =
  | 'large_order'
  | 'iceberg'
  | 'absorption'
  | 'spoofing'
  | 'replenishment'
  | 'liquidity_pulled'
  | 'cluster'
  | 'sweep'
  | 'stop_run'
  | 'imbalance'
  | 'delta_divergence'
  | 'volume_burst'
  | 'vacuum'
  | 'spread_expansion'
  | 'level_setup'
  | 'feed';

export interface MarketEvent {
  id: string;
  t: number;
  kind: EventKind;
  /** Human-facing name, e.g. "Probable Iceberg" */
  title: string;
  /** subtype, e.g. 'replenishment_iceberg' */
  subtype?: string;
  side?: BookSide | 'buy' | 'sell';
  price: number;
  priceHi?: number;
  /** 0..100 */
  confidence: number;
  explain: string;
  data?: Record<string, number | string | boolean>;
  source: SourceId;
  symbol: string;
  /** set when the event is an update of an earlier event (same id) */
  status?: string;
  endT?: number;
}

export type LargeOrderStatus = 'active' | 'partially_filled' | 'filled' | 'pulled' | 'broken';

export interface LargeOrder {
  id: string;
  side: BookSide;
  price: number;
  size: number;          // currently displayed
  peak: number;
  firstSeen: number;
  lastSeen: number;
  holdMs: number;
  replenishments: number;
  executed: number;
  cancelled: number;
  status: LargeOrderStatus;
  confidence: number;
  threshold: number;
  source: SourceId;
}

export type ClusterStatus = 'formed' | 'tested' | 'broken' | 'absorption' | 'faded';

export interface LiquidityCluster {
  id: string;
  side: BookSide;
  lo: number;
  hi: number;
  total: number;
  levels: number;
  density: number;        // total / price width in ticks
  firstSeen: number;
  lastSeen: number;
  executed: number;
  tests: number;
  status: ClusterStatus;
  label: string;          // Bid Liquidity Cluster / ... / Broken Liquidity
  confidence: number;
}

/** One heatmap column: resting liquidity per price bucket, plus flow during the interval. */
export interface HeatColumn {
  t: number;
  dt: number;            // interval length ms
  p0: number;            // price of bucket 0 (lowest)
  step: number;          // bucket size
  n: number;             // number of buckets
  bids: number[];        // resting qty per bucket at column end (0 where none)
  asks: number[];
  /** sparse: [bucket, buyExec, sellExec] */
  exec: [number, number, number][];
  /** sparse: [bucket, qty added] */
  add: [number, number][];
  /** sparse: [bucket, qty cancelled (removed without execution)] */
  rem: [number, number][];
  bb: number;            // best bid at column end
  ba: number;            // best ask at column end
  hi: number;            // highest trade price in interval (0 if none)
  lo: number;
  last: number;          // last trade price or mid
}
