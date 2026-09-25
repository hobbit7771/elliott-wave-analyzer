// Curated liquidity levels for the chart and the heatmap: a few strongest levels as thin horizontal
// lines with small labels, the most important ones in purple. Everything else stays in the Signals tab.
import type { BookSide, LargeOrder, LiquidityCluster, MarketEvent } from '../core/types.js';
import { fmtQ } from './util.js';

export interface LevelMark {
  key: string;
  price: number;
  side: BookSide;
  t0: number;
  /** end time (null = still active, drawn to the right edge) */
  t1: number | null;
  label: string;
  important: boolean;
  /** ranking strength (higher = stronger) */
  strength: number;
  why: string;
}

export const LEVEL_COLORS = { bid: '#26a69a', ask: '#ef5350', important: '#b36bff' } as const;

/**
 * "Important" is deliberately strict, so purple stays rare:
 * - a visible level that is ≥ 2× the dynamic large threshold and has held ≥ 60 s, or has been refilled;
 * - a presumed iceberg / absorption event at a level that is still active with score ≥ 70;
 * - a liquidity cluster that was tested or absorbed flow and still holds (score ≥ 60).
 */
export function selectLevels(
  input: { large: LargeOrder[]; clusters: LiquidityCluster[]; events: MarketEvent[]; now: number; minConf: number },
  max: number,
  tickSize: number,
): LevelMark[] {
  const all: LevelMark[] = [];
  for (const lo of input.large) {
    if (lo.status !== 'active' && lo.status !== 'partially_filled') continue;
    if (lo.confidence < input.minConf) continue;
    const ratio = lo.threshold > 0 ? lo.size / lo.threshold : 1;
    const important = (ratio >= 2 && lo.holdMs >= 60_000) || (lo.replenishments >= 2 && lo.holdMs >= 30_000);
    all.push({
      key: 'L' + lo.id,
      price: lo.price,
      side: lo.side,
      t0: lo.firstSeen,
      t1: null,
      label: `${fmtQ(lo.size)}`,
      important,
      strength: ratio * (1 + Math.min(lo.holdMs, 600_000) / 300_000),
      why: `крупный уровень ${fmtQ(lo.size)} (${ratio.toFixed(1)}× порога), держится ${Math.round(lo.holdMs / 1000)} с`,
    });
  }
  for (const c of input.clusters) {
    if (c.status === 'faded' || c.status === 'broken' || c.confidence < input.minConf) continue;
    const important = (c.status === 'tested' || c.status === 'absorption') && c.confidence >= 60;
    all.push({
      key: 'C' + c.id,
      price: (c.lo + c.hi) / 2,
      side: c.side,
      t0: c.firstSeen,
      t1: null,
      label: `зона ${fmtQ(c.total)}`,
      important,
      strength: 1 + c.confidence / 50 + (important ? 1 : 0),
      why: `${c.label}: ${c.lo}–${c.hi}, объём ${fmtQ(c.total)}`,
    });
  }
  for (const e of input.events) {
    if (e.kind !== 'iceberg' && e.kind !== 'absorption') continue;
    if (e.status === 'broken' || e.status === 'expired' || e.confidence < Math.max(70, input.minConf)) continue;
    if (e.endT !== undefined && input.now - e.endT > 60_000) continue;
    if (input.now - e.t > 30 * 60_000) continue;
    const side: BookSide = e.side === 'ask' || e.side === 'sell' ? 'ask' : 'bid';
    all.push({
      key: 'E' + e.id,
      price: e.price,
      side,
      t0: e.t,
      t1: null,
      label: e.kind === 'iceberg' ? 'айсб.?' : 'поглощ.',
      important: true,
      strength: 3 + e.confidence / 50,
      why: e.title,
    });
  }
  // merge levels closer than ~3 ticks: keep the strongest, carry "important"
  all.sort((a, b) => b.strength - a.strength);
  const kept: LevelMark[] = [];
  const near = Math.max(tickSize * 3, 1e-12);
  for (const l of all) {
    const k = kept.find((x) => Math.abs(x.price - l.price) <= near);
    if (k) {
      k.important ||= l.important;
      continue;
    }
    kept.push({ ...l });
  }
  // all important levels (at most 4), then the strongest ordinary ones up to `max`
  const imp = kept.filter((l) => l.important).slice(0, 4);
  const rest = kept.filter((l) => !l.important).slice(0, Math.max(0, max - imp.length));
  return [...imp, ...rest];
}

/** Hide labels that would overlap vertically (keeps the first = strongest). */
export function labelSlots<T extends { y: number }>(items: T[], minGap = 11): Set<T> {
  const shown = new Set<T>();
  const ys: number[] = [];
  for (const it of items) {
    if (ys.some((y) => Math.abs(y - it.y) < minGap)) continue;
    ys.push(it.y);
    shown.add(it);
  }
  return shown;
}
