// Deterministic replay of a recorded normalized message stream (NDJSON) through the MarketEngine.
// Recording format, one JSON object per line:
//   {"k":"meta","meta":InstrumentMeta,"syncMode":"futures","heatStep":number}
//   {"k":"klines","c":Candle[]}           1m warm-up candles
//   {"k":"snap","s":BookSnapshot}
//   {"k":"diff","d":DepthDiff}
//   {"k":"trade","tr":Trade}
//   {"k":"tick","t":number}               timer tick (wall clock at recording time)
//   {"k":"gate","open":boolean}           freshness gate changes
import type { HeatColumn, MarketEvent, InstrumentMeta } from './types.js';
import { MarketEngine } from './engine.js';
import { DEFAULT_DETECTOR_CONFIG, type DetectorConfig } from './detectors/config.js';
import type { SyncMode } from './orderbook.js';

export type ReplayRecord =
  | { k: 'meta'; meta: InstrumentMeta; syncMode: SyncMode; heatStep: number }
  | { k: 'klines'; c: import('./types.js').Candle[] }
  | { k: 'snap'; s: import('./types.js').BookSnapshot }
  | { k: 'diff'; d: import('./types.js').DepthDiff }
  | { k: 'trade'; tr: import('./types.js').Trade }
  | { k: 'tick'; t: number }
  | { k: 'gate'; open: boolean };

export interface ReplayResult {
  events: MarketEvent[];
  columns: HeatColumn[];
  trades: number;
  diffs: number;
  gaps: number;
  engine: MarketEngine;
}

export function replay(records: Iterable<ReplayRecord>, cfg: DetectorConfig = DEFAULT_DETECTOR_CONFIG): ReplayResult {
  let engine: MarketEngine | null = null;
  const events: MarketEvent[] = [];
  const columns: HeatColumn[] = [];
  const pending: ReplayRecord[] = [];
  for (const r of records) {
    if (r.k === 'meta') {
      engine = new MarketEngine(r.meta, cfg, { syncMode: r.syncMode, heatStep: r.heatStep, heatHalfBuckets: 300, heatIntervalMs: 1000, pruneTicks: 100_000 }, {
        event: (e) => {
          const i = events.findIndex((x) => x.id === e.id);
          if (i >= 0) events[i] = e;
          else events.push(e);
        },
        heat: (c) => columns.push(c),
      });
      engine.beginSync();
      engine.setGate(true);
      continue;
    }
    if (!engine) {
      pending.push(r);
      continue;
    }
    switch (r.k) {
      case 'klines':
        engine.seedCandles(r.c);
        break;
      case 'snap':
        engine.onSnapshot(r.s);
        break;
      case 'diff':
        engine.onDiff(r.d);
        break;
      case 'trade':
        engine.onTrade(r.tr);
        break;
      case 'tick':
        engine.onTimer(r.t);
        break;
      case 'gate':
        engine.setGate(r.open);
        break;
    }
  }
  if (!engine) throw new Error('recording has no meta record');
  return { events, columns, trades: engine.trades, diffs: engine.diffs, gaps: engine.gaps, engine };
}

export function parseNdjson(text: string): ReplayRecord[] {
  return text
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as ReplayRecord);
}
