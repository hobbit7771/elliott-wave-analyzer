import type { EventKind, MarketEvent } from '../core/types.js';

/** Owner token (from Render env OWNER_TOKEN) kept only in this browser; sent for owner-only actions. */
export function ownerToken(): string {
  try {
    return localStorage.getItem('oft:owner') ?? '';
  } catch {
    return '';
  }
}

export async function api<T>(path: string, params: Record<string, string | number | undefined> = {}, init?: RequestInit): Promise<T> {
  const q = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join('&');
  const headers = new Headers(init?.headers);
  const tok = ownerToken();
  if (tok) headers.set('x-oft-owner', tok);
  if (init?.body && !headers.has('content-type')) headers.set('content-type', 'application/json');
  const r = await fetch(path + (q ? '?' + q : ''), { ...init, headers, signal: init?.signal });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((body as { error?: string }).error ?? `HTTP ${r.status}`);
  return body as T;
}

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Record<string, string> = {}, ...children: (Node | string)[]): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

export function h(html: string): HTMLElement {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild as HTMLElement;
}

export const esc = (s: string): string => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);

export function fmtQ(v: number): string {
  if (!Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (a >= 1e4) return (v / 1e3).toFixed(1) + 'k';
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  if (a === 0) return '0';
  return v.toFixed(3);
}

export function fmtP(v: number, dec: number): string {
  return Number.isFinite(v) ? v.toFixed(dec) : '–';
}

export function fmtTime(t: number, withMs = false): string {
  const d = new Date(t);
  const s = d.toISOString().slice(11, withMs ? 23 : 19);
  return s;
}
export function fmtDateTime(t: number): string {
  return new Date(t).toISOString().replace('T', ' ').slice(0, 19) + 'Z';
}
export function ago(ms: number): string {
  if (!isFinite(ms) || ms < 0) return '–';
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

export function confColor(c: number): string {
  if (c >= 75) return '#b388ff';
  if (c >= 60) return '#4ea1ff';
  if (c >= 45) return '#ffb300';
  return '#8a93a6';
}

export const KIND_LABEL: Record<EventKind, string> = {
  large_order: 'Крупный уровень',
  iceberg: 'Предполагаемый айсберг',
  absorption: 'Поглощение',
  spoofing: 'Подозрение на спуфинг',
  replenishment: 'Признаки пополнения',
  liquidity_pulled: 'Снятие ликвидности',
  cluster: 'Кластер ликвидности',
  sweep: 'Sweep',
  stop_run: 'Stop-run',
  imbalance: 'Дисбаланс',
  delta_divergence: 'Дивергенция дельты',
  volume_burst: 'Всплеск объёма',
  vacuum: 'Вакуум ликвидности',
  spread_expansion: 'Расширение спреда',
  feed: 'Состояние данных',
};

export const KIND_COLOR: Record<EventKind, string> = {
  large_order: '#4ea1ff',
  iceberg: '#b388ff',
  absorption: '#ff9800',
  spoofing: '#9e9e9e',
  replenishment: '#b388ff',
  liquidity_pulled: '#e0e0e0',
  cluster: '#ffd54f',
  sweep: '#00e5ff',
  stop_run: '#ff4081',
  imbalance: '#8bc34a',
  delta_divergence: '#f06292',
  volume_burst: '#ffee58',
  vacuum: '#90a4ae',
  spread_expansion: '#ffab40',
  feed: '#ef5350',
};

export function isBullish(e: MarketEvent): boolean | null {
  if (e.side === 'bid' || e.side === 'buy') return true;
  if (e.side === 'ask' || e.side === 'sell') return false;
  return null;
}

/** rAF-throttled renderer that only runs when visible and dirty. */
export class Painter {
  private dirty = false;
  private raf = 0;
  visible = false;
  private last = 0;
  constructor(private fn: () => void, private minIntervalMs = 0) {}
  mark(): void {
    this.dirty = true;
    this.schedule();
  }
  private schedule(): void {
    if (!this.visible || this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      if (!this.dirty || !this.visible) return;
      const now = performance.now();
      if (now - this.last < this.minIntervalMs) {
        setTimeout(() => this.schedule(), this.minIntervalMs - (now - this.last));
        return;
      }
      this.last = now;
      this.dirty = false;
      this.fn();
    });
  }
  show(): void {
    this.visible = true;
    this.mark();
  }
  hide(): void {
    this.visible = false;
  }
}

/** Resize a canvas to its CSS box at device pixel ratio; returns ctx scaled to CSS pixels. */
export function fitCanvas(c: HTMLCanvasElement): { ctx: CanvasRenderingContext2D; w: number; h: number; dpr: number } {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const r = c.getBoundingClientRect();
  const w = Math.max(1, Math.round(r.width));
  const hh = Math.max(1, Math.round(r.height));
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(hh * dpr)) {
    c.width = Math.round(w * dpr);
    c.height = Math.round(hh * dpr);
  }
  const ctx = c.getContext('2d')!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hh, dpr };
}

export function download(name: string, data: string, type = 'text/plain'): void {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([data], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

export function loadPref<T>(k: string, d: T): T {
  try {
    const v = localStorage.getItem('oft:' + k);
    return v ? { ...d, ...JSON.parse(v) } : d;
  } catch {
    return d;
  }
}
export function loadPrefRaw<T>(k: string, d: T): T {
  try {
    const v = localStorage.getItem('oft:' + k);
    return v ? (JSON.parse(v) as T) : d;
  } catch {
    return d;
  }
}
export function savePref(k: string, v: unknown): void {
  try {
    localStorage.setItem('oft:' + k, JSON.stringify(v));
  } catch {
    /* storage unavailable */
  }
}

/** Pointer drag + wheel + pinch helper for canvases. */
export function gestures(
  c: HTMLElement,
  o: {
    drag?: (dx: number, dy: number, e: PointerEvent) => void;
    zoom?: (factor: number, x: number, y: number, axis: 'x' | 'y') => void;
    hover?: (x: number, y: number) => void;
    leave?: () => void;
    tap?: (x: number, y: number) => void;
  },
): void {
  const pts = new Map<number, { x: number; y: number }>();
  let pinch0 = 0;
  let moved = 0;
  c.addEventListener('pointerdown', (e) => {
    c.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved = 0;
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch0 = Math.hypot(a.x - b.x, a.y - b.y);
    }
  });
  c.addEventListener('pointermove', (e) => {
    const r = c.getBoundingClientRect();
    const p = pts.get(e.pointerId);
    if (!p) {
      o.hover?.(e.clientX - r.left, e.clientY - r.top);
      return;
    }
    if (pts.size === 2) {
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch0 > 0 && d > 0) {
        const horiz = Math.abs(a.x - b.x) > Math.abs(a.y - b.y);
        o.zoom?.(pinch0 / d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, horiz ? 'x' : 'y');
      }
      pinch0 = d;
      return;
    }
    const dx = e.clientX - p.x;
    const dy = e.clientY - p.y;
    moved += Math.abs(dx) + Math.abs(dy);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    o.drag?.(dx, dy, e);
    o.hover?.(e.clientX - r.left, e.clientY - r.top);
  });
  const end = (e: PointerEvent) => {
    const r = c.getBoundingClientRect();
    if (pts.size === 1 && moved < 5) o.tap?.(e.clientX - r.left, e.clientY - r.top);
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch0 = 0;
  };
  c.addEventListener('pointerup', end);
  c.addEventListener('pointercancel', end);
  c.addEventListener('pointerleave', () => o.leave?.());
  c.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      const f = Math.exp(Math.sign(e.deltaY) * Math.min(0.3, Math.abs(e.deltaY) / 400));
      o.zoom?.(f, e.clientX - r.left, e.clientY - r.top, e.shiftKey || e.altKey ? 'y' : 'x');
    },
    { passive: false },
  );
}
