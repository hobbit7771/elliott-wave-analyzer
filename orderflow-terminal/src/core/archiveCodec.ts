// Raw L2 archive block: snapshot of the synced local book inside a fixed price band + every depth diff
// (restricted to that band) + every trade, for one continuous interval without sequence gaps.
// Replaying a block reproduces the published aggregated L2 state inside the band exactly.
import { gzipSync, gunzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';
import type { BookSnapshot, DepthDiff, Level, Trade } from './types.js';
import { OrderBook } from './orderbook.js';
import { toTick, fromTick, decimalsOf } from './precision.js';

export const ARCHIVE_SCHEMA_VERSION = 1;

export interface ArchiveBlock {
  v: number;
  source: string;
  symbol: string;
  tick: number;
  t0: number;
  t1: number;
  /** fixed price band [lo, hi] (in ticks) fixed at block start */
  loTick: number;
  hiTick: number;
  snapshot: { lastUpdateId: number; t: number; bids: [number, number][]; asks: [number, number][] };
  /** [t, U, u, pu|null, bids[[tick,qty]], asks[[tick,qty]]] */
  diffs: [number, number, number, number | null, [number, number][], [number, number][]][];
  /** [t, priceTick, qty, side, id] */
  trades: [number, number, number, 1 | -1, number][];
}

export class BlockBuilder {
  block: ArchiveBlock;
  bytesEstimate = 0;
  constructor(source: string, symbol: string, tick: number, book: OrderBook, t0: number, bandFrac: number) {
    const mid = (book.bestBid + book.bestAsk) / 2;
    const lo = Math.max(toTick(mid * (1 - bandFrac), tick), book.reliableLo);
    const hi = Math.min(toTick(mid * (1 + bandFrac), tick), book.reliableHi);
    const bids: [number, number][] = [];
    const asks: [number, number][] = [];
    for (const [k, q] of book.bids) if (k >= lo && k <= hi) bids.push([k, q]);
    for (const [k, q] of book.asks) if (k >= lo && k <= hi) asks.push([k, q]);
    bids.sort((a, b) => b[0] - a[0]);
    asks.sort((a, b) => a[0] - b[0]);
    this.block = { v: ARCHIVE_SCHEMA_VERSION, source, symbol, tick, t0, t1: t0, loTick: lo, hiTick: hi, snapshot: { lastUpdateId: book.lastUpdateId, t: t0, bids, asks }, diffs: [], trades: [] };
    this.bytesEstimate = (bids.length + asks.length) * 14;
  }

  addDiff(d: DepthDiff): void {
    const b = this.block;
    const f = (ls: Level[]): [number, number][] => {
      const out: [number, number][] = [];
      for (const [p, q] of ls) {
        const k = toTick(p, b.tick);
        if (k >= b.loTick && k <= b.hiTick) out.push([k, q]);
      }
      return out;
    };
    const bids = f(d.bids);
    const asks = f(d.asks);
    b.diffs.push([d.t, d.firstId, d.lastId, d.prevLastId ?? null, bids, asks]);
    b.t1 = Math.max(b.t1, d.t);
    this.bytesEstimate += 24 + (bids.length + asks.length) * 14;
  }

  addTrade(tr: Trade): void {
    const b = this.block;
    b.trades.push([tr.t, toTick(tr.price, b.tick), tr.qty, tr.side, tr.id ?? 0]);
    b.t1 = Math.max(b.t1, tr.t);
    this.bytesEstimate += 30;
  }

  get empty(): boolean {
    return !this.block.diffs.length && !this.block.trades.length;
  }
}

export interface EncodedBlock {
  bytes: Uint8Array;
  sha256: string;
  path: string;
}

export function blockPath(b: ArchiveBlock): string {
  const d = new Date(b.t0);
  const day = d.toISOString().slice(0, 10);
  return `${b.source}/${b.symbol}/${day}/l2_${b.t0}_${b.snapshot.lastUpdateId}.json.gz`;
}

export function encodeBlock(b: ArchiveBlock): EncodedBlock {
  const bytes = gzipSync(Buffer.from(JSON.stringify(b)), { level: 6 });
  return { bytes, sha256: createHash('sha256').update(bytes).digest('hex'), path: blockPath(b) };
}

export function decodeBlock(bytes: Uint8Array, expectSha?: string): ArchiveBlock {
  if (expectSha) {
    const sha = createHash('sha256').update(bytes).digest('hex');
    if (sha !== expectSha) throw new Error(`archive checksum mismatch ${sha} != ${expectSha}`);
  }
  const b = JSON.parse(gunzipSync(bytes).toString()) as ArchiveBlock;
  if (b.v !== ARCHIVE_SCHEMA_VERSION) throw new Error(`unsupported archive schema ${b.v}`);
  return b;
}

/**
 * Replay a block into a local book. Returns the book state at `until` (or at block end) and
 * whether the diff sequence was continuous. Levels outside [loTick, hiTick] are never known.
 */
export function replayBlock(b: ArchiveBlock, until = Infinity, futures = true): { book: OrderBook; continuous: boolean; appliedDiffs: number; lastUpdateId: number } {
  const book = new OrderBook(b.tick);
  const dec = decimalsOf(b.tick);
  const snap: BookSnapshot = {
    lastUpdateId: b.snapshot.lastUpdateId,
    t: b.snapshot.t,
    bids: b.snapshot.bids.map(([k, q]) => [fromTick(k, b.tick, dec), q]),
    asks: b.snapshot.asks.map(([k, q]) => [fromTick(k, b.tick, dec), q]),
  };
  book.applySnapshot(snap);
  book.reliableLo = b.loTick;
  book.reliableHi = b.hiTick;
  let lastU = b.snapshot.lastUpdateId;
  let continuous = true;
  let applied = 0;
  for (const [t, U, u, pu, bids, asks] of b.diffs) {
    if (t > until) break;
    // the block snapshot is the local book right after a diff, so the very first diff must chain too
    const ok = futures ? pu === lastU : U === lastU + 1;
    if (!ok) continuous = false;
    book.applyDiff({
      t,
      firstId: U,
      lastId: u,
      prevLastId: pu ?? undefined,
      bids: bids.map(([k, q]) => [fromTick(k, b.tick, dec), q]),
      asks: asks.map(([k, q]) => [fromTick(k, b.tick, dec), q]),
    });
    lastU = u;
    applied++;
  }
  return { book, continuous, appliedDiffs: applied, lastUpdateId: lastU };
}

export function blockTrades(b: ArchiveBlock): Trade[] {
  const dec = decimalsOf(b.tick);
  return b.trades.map(([t, k, qty, side, id]) => ({ t, price: fromTick(k, b.tick, dec), qty, side, id: id || undefined }));
}
