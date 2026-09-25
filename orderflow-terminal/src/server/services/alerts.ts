// Server-side alert rules: evaluated on every live detector / feed event while the server runs,
// journaled in Supabase (idempotent per rule+event), pushed to connected browsers.
import type { EventKind, MarketEvent } from '../../core/types.js';
import { TF_MS, type Timeframe } from '../../core/candles.js';

export interface AlertRule {
  id: number;
  enabled: boolean;
  symbol: string; // '' = any
  kinds: EventKind[]; // [] = any
  minScore: number;
  minVolume: number;
  tf: Timeframe | ''; // at most one alert per bar of this timeframe
  cooldownMs: number;
  sound: boolean;
  notify: boolean;
}

export const DEFAULT_RULES: AlertRule[] = [
  { id: 1, enabled: true, symbol: '', kinds: ['iceberg', 'absorption'], minScore: 70, minVolume: 0, tf: '', cooldownMs: 60_000, sound: true, notify: true },
  { id: 2, enabled: true, symbol: '', kinds: ['feed'], minScore: 0, minVolume: 0, tf: '', cooldownMs: 30_000, sound: false, notify: true },
];

export interface FiredAlert {
  t: number;
  ruleId: number;
  event: MarketEvent;
  sound: boolean;
  notify: boolean;
}

export function eventSize(ev: MarketEvent): number {
  const d = ev.data ?? {};
  for (const k of ['size', 'volume', 'peak', 'total', 'traded']) if (typeof d[k] === 'number') return d[k] as number;
  return NaN;
}

export class AlertService {
  private lastBar = new Map<number, number>();
  private lastFire = new Map<string, number>();
  private fired = new Set<string>();
  constructor(
    public rules: AlertRule[],
    private logAlert: (a: FiredAlert) => Promise<boolean>,
    private broadcast: (a: FiredAlert) => void,
  ) {}

  matches(r: AlertRule, ev: MarketEvent): boolean {
    if (!r.enabled) return false;
    if (r.symbol && r.symbol !== ev.symbol) return false;
    if (r.kinds.length && !r.kinds.includes(ev.kind)) return false;
    if (ev.kind !== 'feed' && ev.confidence < r.minScore) return false;
    if (r.minVolume > 0 && !(eventSize(ev) >= r.minVolume)) return false;
    return true;
  }

  /** Returns the alerts fired for this event (after dedupe, per-bar limit and cooldown). */
  onEvent(ev: MarketEvent, now = Date.now()): FiredAlert[] {
    const out: FiredAlert[] = [];
    for (const r of this.rules) {
      if (!this.matches(r, ev)) continue;
      const k = `${r.id}|${ev.id}`;
      if (this.fired.has(k)) continue; // updates of the same event do not re-alert
      const ck = `${r.id}|${ev.symbol}|${ev.kind}`;
      if (now - (this.lastFire.get(ck) ?? 0) < r.cooldownMs) continue;
      if (r.tf && r.tf !== 'tick') {
        const bar = Math.floor(ev.t / TF_MS[r.tf]);
        if (this.lastBar.get(r.id) === bar) continue;
        this.lastBar.set(r.id, bar);
      }
      this.fired.add(k);
      if (this.fired.size > 50_000) this.fired.clear();
      this.lastFire.set(ck, now);
      const a: FiredAlert = { t: now, ruleId: r.id, event: ev, sound: r.sound, notify: r.notify };
      out.push(a);
      void this.logAlert(a).catch(() => {});
      this.broadcast(a);
    }
    return out;
  }
}
