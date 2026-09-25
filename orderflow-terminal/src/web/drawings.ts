// Пользовательские объекты графика: горизонтальные уровни, трендовые линии, Фибоначчи.
// Хранятся на сервере (Supabase, только для владельца) с резервной копией в браузере.
import type { ISeriesPrimitive, SeriesAttachedParameter, SeriesType, Time, UTCTimestamp } from 'lightweight-charts';
import { api, loadPrefRaw, ownerToken, savePref } from './util.js';

export type DrawingType = 'hline' | 'trend' | 'fib' | 'fibext';
export interface Pt {
  t: number; // epoch ms (real time, independent of timeframe)
  p: number;
}
export interface Drawing {
  id: string;
  type: DrawingType;
  a: Pt;
  b?: Pt;
}

export const FIB_RET = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
export const FIB_EXT = [1, 1.272, 1.414, 1.618, 2, 2.618];

/** Fibonacci price levels for A→B (retracement measured back from B, extension projected beyond B). */
export function fibLevels(d: Drawing): { r: number; price: number }[] {
  if (!d.b) return [];
  const range = d.b.p - d.a.p;
  if (d.type === 'fib') return FIB_RET.map((r) => ({ r, price: d.b!.p - range * r }));
  if (d.type === 'fibext') return FIB_EXT.map((r) => ({ r, price: d.a.p + range * r }));
  return [];
}

export async function loadDrawings(key: string): Promise<{ list: Drawing[]; where: 'server' | 'browser' }> {
  if (ownerToken()) {
    try {
      const list = await api<Drawing[]>('/api/objects', { source: key.split(':')[0], symbol: key.split(':')[1] });
      savePref('draw:' + key, list);
      return { list, where: 'server' };
    } catch {
      /* fall back to the local copy */
    }
  }
  return { list: loadPrefRaw<Drawing[]>('draw:' + key, []), where: 'browser' };
}

export async function saveDrawings(key: string, list: Drawing[]): Promise<'server' | 'browser'> {
  savePref('draw:' + key, list);
  if (!ownerToken()) return 'browser';
  try {
    await api('/api/objects', { source: key.split(':')[0], symbol: key.split(':')[1] }, { method: 'PUT', body: JSON.stringify(list) });
    return 'server';
  } catch {
    return 'browser';
  }
}

/** Renders trend lines and Fibonacci sets on the price pane. Horizontal levels use native price lines. */
export class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  list: Drawing[] = [];
  pending: Pt | null = null;
  private p: SeriesAttachedParameter<Time, SeriesType> | null = null;
  constructor(private toTime: (ms: number) => number | null) {}
  attached(p: SeriesAttachedParameter<Time, SeriesType>): void {
    this.p = p;
  }
  detached(): void {
    this.p = null;
  }
  update(list: Drawing[]): void {
    this.list = list;
    this.p?.requestUpdate();
  }
  paneViews() {
    const self = this;
    return [
      {
        zOrder: () => 'top' as const,
        renderer: () => ({
          draw: (target: { useMediaCoordinateSpace: (f: (s: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => void) => void }) => {
            const p = self.p;
            if (!p) return;
            target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
              try {
              const ts = p.chart.timeScale();
              const x = (ms: number): number | null => {
                const t = self.toTime(ms);
                return t === null ? null : ts.timeToCoordinate(t as UTCTimestamp);
              };
              const y = (price: number) => p.series.priceToCoordinate(price);
              ctx.font = '10px sans-serif';
              for (const d of self.list) {
                if (d.type === 'hline' || !d.b) continue;
                const xa = x(d.a.t);
                const xb = x(d.b.t);
                if (d.type === 'trend') {
                  const ya = y(d.a.p);
                  const yb = y(d.b.p);
                  if (xa === null || xb === null || ya === null || yb === null) continue;
                  ctx.strokeStyle = '#4ea1ff';
                  ctx.lineWidth = 1.5;
                  ctx.beginPath();
                  ctx.moveTo(xa, ya);
                  ctx.lineTo(xb, yb);
                  ctx.stroke();
                  continue;
                }
                const x0 = Math.min(xa ?? 0, xb ?? 0);
                for (const { r, price } of fibLevels(d)) {
                  const yy = y(price);
                  if (yy === null) continue;
                  ctx.strokeStyle = d.type === 'fib' ? 'rgba(255,213,79,0.8)' : 'rgba(206,147,216,0.8)';
                  ctx.lineWidth = 1;
                  ctx.beginPath();
                  ctx.moveTo(x0, yy);
                  ctx.lineTo(mediaSize.width, yy);
                  ctx.stroke();
                  ctx.fillStyle = ctx.strokeStyle;
                  ctx.fillText(`${r} (${price.toFixed(2)})`, x0 + 3, yy - 2);
                }
              }
              if (self.pending) {
                const xa = x(self.pending.t);
                const ya = y(self.pending.p);
                if (xa !== null && ya !== null) {
                  ctx.fillStyle = '#fff';
                  ctx.beginPath();
                  ctx.arc(xa, ya, 4, 0, Math.PI * 2);
                  ctx.fill();
                }
              }
              } catch {
                /* series not ready (data being replaced): skip this frame */
              }
            });
          },
        }),
      },
    ];
  }
}
