// Alerts: user rules over real detector / feed events -> browser notifications and sound.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { el, esc, fmtP, fmtDateTime, KIND_LABEL, loadPrefRaw, savePref } from '../util.js';
import { TIMEFRAMES, TF_LABEL, TF_MS, type Timeframe } from '../../core/candles.js';
import type { EventKind, MarketEvent } from '../../core/types.js';

export interface AlertRule {
  id: number;
  enabled: boolean;
  symbol: string; // '' = any instrument
  kinds: EventKind[];
  minConfidence: number;
  minVolume: number; // applies to events that carry a size (size / volume / peak / estimatedHidden / total)
  tf: Timeframe | ''; // max one notification per bar of this timeframe per rule
  sound: boolean;
  notify: boolean;
}

const FEED_KINDS: EventKind[] = ['feed', 'spread_expansion'];
const DEFAULT_RULES: AlertRule[] = [
  { id: 1, enabled: true, symbol: '', kinds: ['iceberg'], minConfidence: 70, minVolume: 0, tf: '', sound: true, notify: true },
  { id: 2, enabled: true, symbol: '', kinds: ['feed'], minConfidence: 0, minVolume: 0, tf: '', sound: false, notify: true },
];

class AlertEngine {
  rules: AlertRule[] = loadPrefRaw('alertRules', DEFAULT_RULES);
  log: { t: number; rule: number; ev: MarketEvent }[] = [];
  private lastBar = new Map<number, number>();
  private audio: AudioContext | null = null;
  onChange: () => void = () => {};

  save(): void {
    savePref('alertRules', this.rules);
  }

  eventSize(ev: MarketEvent): number {
    const d = ev.data ?? {};
    for (const k of ['size', 'volume', 'peak', 'estimatedHidden', 'total', 'traded']) if (typeof d[k] === 'number') return d[k] as number;
    return NaN;
  }

  matches(r: AlertRule, ev: MarketEvent): boolean {
    if (!r.enabled) return false;
    if (r.symbol && r.symbol !== ev.symbol) return false;
    if (r.kinds.length && !r.kinds.includes(ev.kind)) return false;
    if (!FEED_KINDS.includes(ev.kind) && ev.confidence < r.minConfidence) return false;
    if (r.minVolume > 0) {
      const s = this.eventSize(ev);
      if (!(s >= r.minVolume)) return false;
    }
    return true;
  }

  onEvent(ev: MarketEvent, isNew: boolean): void {
    // updates of an existing event only alert when they newly qualify (e.g. confidence rose)
    if (!isNew && ev.kind !== 'iceberg') return;
    for (const r of this.rules) {
      if (!this.matches(r, ev)) continue;
      if (!isNew && this.log.some((l) => l.rule === r.id && l.ev.id === ev.id)) continue;
      if (r.tf && r.tf !== 'tick') {
        const bar = Math.floor(ev.t / TF_MS[r.tf]);
        if (this.lastBar.get(r.id) === bar) continue;
        this.lastBar.set(r.id, bar);
      }
      this.fire(r, ev);
    }
  }

  fire(r: AlertRule, ev: MarketEvent): void {
    this.log.unshift({ t: Date.now(), rule: r.id, ev });
    if (this.log.length > 300) this.log.pop();
    const title = `${ev.symbol}: ${ev.title}${ev.kind === 'feed' ? '' : ` (${ev.confidence})`}`;
    const body = `${isFinite(ev.price) ? '@ ' + ev.price + ' — ' : ''}${ev.explain}`.slice(0, 240);
    if (r.sound) this.beep(ev.kind === 'feed' ? 330 : 880);
    if (r.notify && 'Notification' in window && Notification.permission === 'granted') {
      const opts = { body, tag: ev.id, icon: '/icon.svg' };
      void navigator.serviceWorker?.getRegistration().then((reg) => {
        if (reg) void reg.showNotification(title, opts);
        else new Notification(title, opts);
      });
    }
    this.onChange();
  }

  beep(freq: number): void {
    try {
      this.audio ??= new AudioContext();
      const o = this.audio.createOscillator();
      const g = this.audio.createGain();
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.15, this.audio.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, this.audio.currentTime + 0.35);
      o.connect(g).connect(this.audio.destination);
      o.start();
      o.stop(this.audio.currentTime + 0.35);
    } catch {
      /* audio unavailable */
    }
  }
}

export const alerts = new AlertEngine();

export function createAlertsTab(): Tab {
  const root = el('section', { id: 'tab-alerts', role: 'tabpanel' });
  const permBtn = el('button', { text: 'Enable browser notifications' });
  const testBtn = el('button', { text: 'Test sound' });
  const addBtn = el('button', { text: '+ Rule' });
  const permState = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, permBtn, testBtn, addBtn, permState));
  const body = el('div', { class: 'scroll pad' });
  const rulesBox = el('div');
  const logBox = el('div');
  body.append(
    el('p', { class: 'muted', text: 'Rules fire on real detector events (large limit order, probable iceberg, absorption, cluster formed, liquidity removed, sweep, imbalance, …) and on feed events (order-book disconnect, stale data, resynchronization, data gap, spread expansion). On iPhone, notifications require adding the app to the Home Screen (iOS 16.4+) and allowing them.' }),
    rulesBox,
    el('h3', { text: 'Fired alerts' }),
    logBox,
  );
  root.append(body);

  function perm(): void {
    permState.textContent = 'Notification' in window ? `Notifications: ${Notification.permission}` : 'Notifications not supported in this browser (sound alerts still work).';
  }
  permBtn.onclick = async () => {
    if ('Notification' in window) await Notification.requestPermission();
    alerts.beep(660);
    perm();
  };
  testBtn.onclick = () => alerts.beep(880);
  addBtn.onclick = () => {
    alerts.rules.push({ id: Math.max(0, ...alerts.rules.map((r) => r.id)) + 1, enabled: true, symbol: store.symbol, kinds: ['absorption'], minConfidence: 60, minVolume: 0, tf: '', sound: true, notify: true });
    alerts.save();
    render();
  };

  function render(): void {
    rulesBox.replaceChildren(...alerts.rules.map(ruleRow));
    renderLog();
  }

  function ruleRow(r: AlertRule): HTMLElement {
    const upd = () => {
      alerts.save();
    };
    const en = el('input', { type: 'checkbox' });
    en.checked = r.enabled;
    en.onchange = () => ((r.enabled = en.checked), upd());
    const sym = el('input', { value: r.symbol, placeholder: 'any', style: 'width:110px;text-transform:uppercase' });
    sym.onchange = () => ((r.symbol = sym.value.trim().toUpperCase()), upd());
    const kinds = el('select', { multiple: 'true', size: '4' });
    for (const [k, l] of Object.entries(KIND_LABEL)) kinds.append(el('option', { value: k, text: l, ...(r.kinds.includes(k as EventKind) ? { selected: 'true' } : {}) }));
    kinds.onchange = () => ((r.kinds = [...kinds.selectedOptions].map((o) => o.value as EventKind)), upd());
    const conf = el('input', { type: 'number', min: '0', max: '100', step: '5', value: String(r.minConfidence) });
    conf.onchange = () => ((r.minConfidence = +conf.value), upd());
    const vol = el('input', { type: 'number', min: '0', step: 'any', value: String(r.minVolume) });
    vol.onchange = () => ((r.minVolume = +vol.value || 0), upd());
    const tf = el('select');
    tf.append(el('option', { value: '', text: 'every event' }));
    for (const t of TIMEFRAMES) if (t !== 'tick') tf.append(el('option', { value: t, text: `≤1 per ${TF_LABEL[t]} bar` }));
    tf.value = r.tf;
    tf.onchange = () => ((r.tf = tf.value as Timeframe | ''), upd());
    const snd = el('input', { type: 'checkbox' });
    snd.checked = r.sound;
    snd.onchange = () => ((r.sound = snd.checked), upd());
    const ntf = el('input', { type: 'checkbox' });
    ntf.checked = r.notify;
    ntf.onchange = () => ((r.notify = ntf.checked), upd());
    const del = el('button', { text: 'Delete' });
    del.onclick = () => {
      alerts.rules = alerts.rules.filter((x) => x !== r);
      alerts.save();
      render();
    };
    return el(
      'div',
      { class: 'card', style: 'margin-bottom:8px' },
      el('div', { class: 'form-grid' }, el('label', {}, en, `Rule #${r.id} enabled`), el('label', {}, 'Instrument', sym), el('label', {}, 'Event types', kinds), el('label', {}, 'Min confidence', conf), el('label', {}, 'Min volume', vol), el('label', {}, 'Timeframe', tf), el('label', {}, snd, 'Sound'), el('label', {}, ntf, 'Browser / push notification'), del),
    );
  }

  function renderLog(): void {
    logBox.innerHTML = alerts.log.length
      ? '<table><thead><tr><th class="l">Fired</th><th class="l">Instrument</th><th class="l">Event</th><th>Price</th><th>Conf</th><th class="l">Why</th></tr></thead><tbody>' +
        alerts.log.map((l) => `<tr><td class="l">${fmtDateTime(l.t)}</td><td class="l">${esc(l.ev.symbol)}</td><td class="l">${esc(l.ev.title)}</td><td>${fmtP(l.ev.price, store.meta?.pricePrecision ?? 2)}</td><td>${l.ev.confidence}</td><td class="wrap">${esc(l.ev.explain)}</td></tr>`).join('') +
        '</tbody></table>'
      : '<p class="muted">No alerts fired yet.</p>';
  }
  alerts.onChange = () => renderLog();
  perm();
  render();
  return { id: 'alerts', title: 'Alerts', root, show: () => (perm(), renderLog()), hide: () => {} };
}
