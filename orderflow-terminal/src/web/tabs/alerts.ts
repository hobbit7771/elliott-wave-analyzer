// Алерты: правила выполняются на сервере (пока Render работает), журнал хранится в Supabase.
// Браузер показывает уведомление и проигрывает звук, когда сервер присылает сработавший алерт.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, esc, fmtP, fmtDateTime, KIND_LABEL, ownerToken } from '../util.js';
import { TIMEFRAMES, TF_LABEL, type Timeframe } from '../../core/candles.js';
import type { EventKind, MarketEvent } from '../../core/types.js';

interface AlertRule {
  id: number;
  enabled: boolean;
  symbol: string;
  kinds: EventKind[];
  minScore: number;
  minVolume: number;
  tf: Timeframe | '';
  cooldownMs: number;
  sound: boolean;
  notify: boolean;
}
interface ServerAlert {
  t: number;
  ruleId: number;
  event: MarketEvent;
  sound: boolean;
  notify: boolean;
}

class AlertClient {
  recent: ServerAlert[] = [];
  private audio: AudioContext | null = null;
  onChange: () => void = () => {};

  /** A rule fired on the server. Replay never produces these (replay runs only in the browser). */
  onServerAlert(a: ServerAlert): void {
    this.recent.unshift(a);
    if (this.recent.length > 200) this.recent.pop();
    const ev = a.event;
    if (a.sound) this.beep(ev.kind === 'feed' ? 330 : 880);
    if (a.notify && 'Notification' in window && Notification.permission === 'granted') {
      const title = `${ev.symbol}: ${ev.title}${ev.kind === 'feed' ? '' : ` (score ${ev.confidence})`}`;
      const opts = { body: `${isFinite(ev.price) ? '@ ' + ev.price + ' — ' : ''}${ev.explain}`.slice(0, 240), tag: ev.id, icon: '/icon.svg' };
      void navigator.serviceWorker?.getRegistration().then((reg) => (reg ? void reg.showNotification(title, opts) : new Notification(title, opts)));
    }
    this.onChange();
  }

  /** local browser-connection loss (the server cannot report its own unreachability) */
  onEvent(ev: MarketEvent, _isNew: boolean): void {
    this.onServerAlert({ t: Date.now(), ruleId: 0, event: ev, sound: false, notify: true });
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

export const alerts = new AlertClient();

export function createAlertsTab(): Tab {
  const root = el('section', { id: 'tab-alerts', role: 'tabpanel' });
  const permBtn = el('button', { text: 'Разрешить уведомления' });
  const testBtn = el('button', { text: 'Проверить звук' });
  const addBtn = el('button', { text: '+ Правило' });
  const saveBtn = el('button', { text: 'Сохранить правила' });
  const info = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, permBtn, testBtn, addBtn, saveBtn, info));
  const body = el('div', { class: 'scroll pad' });
  const rulesBox = el('div');
  const logBox = el('div');
  body.append(
    el('p', {
      class: 'muted',
      text: 'Правила проверяет сервер на каждом событии детекторов и потока данных (крупный уровень, кластер, предполагаемый айсберг, поглощение, sweep, дисбаланс, снятие ликвидности, расширение спреда, разрыв/устаревание/ресинхронизация, ошибки записи истории), пока сервис Render работает. Журнал хранится в Supabase. Push при закрытом приложении НЕ реализован: уведомления приходят, пока терминал открыт (на iPhone — после «На экран Домой», iOS 16.4+). Во время сна Render правила не проверяются.',
    }),
    rulesBox,
    el('h3', { text: 'Сработавшие алерты' }),
    logBox,
  );
  root.append(body);
  let rules: AlertRule[] = [];
  let log: { t: number; rule_id: number; symbol: string; body: ServerAlert }[] = [];

  async function load(): Promise<void> {
    if (!ownerToken()) {
      info.textContent = 'Нужен токен владельца (вкладка «Источники и настройки»).';
      rulesBox.textContent = '';
      renderLog();
      return;
    }
    try {
      const r = await api<{ rules: AlertRule[]; log: typeof log }>('/api/alerts');
      rules = r.rules;
      log = r.log;
      info.textContent = '';
      render();
    } catch (e) {
      info.textContent = 'Не удалось загрузить: ' + (e as Error).message;
    }
  }

  function render(): void {
    rulesBox.replaceChildren(...rules.map(ruleRow));
    renderLog();
  }

  function ruleRow(r: AlertRule): HTMLElement {
    const en = el('input', { type: 'checkbox' });
    en.checked = r.enabled;
    en.onchange = () => (r.enabled = en.checked);
    const sym = el('input', { value: r.symbol, placeholder: 'любой', style: 'width:110px;text-transform:uppercase' });
    sym.onchange = () => (r.symbol = sym.value.trim().toUpperCase());
    const kinds = el('select', { multiple: 'true', size: '5' });
    for (const [k, l] of Object.entries(KIND_LABEL)) kinds.append(el('option', { value: k, text: l, ...(r.kinds.includes(k as EventKind) ? { selected: 'true' } : {}) }));
    kinds.onchange = () => (r.kinds = [...kinds.selectedOptions].map((o) => o.value as EventKind));
    const score = el('input', { type: 'number', min: '0', max: '100', step: '5', value: String(r.minScore) });
    score.onchange = () => (r.minScore = +score.value);
    const vol = el('input', { type: 'number', min: '0', step: 'any', value: String(r.minVolume) });
    vol.onchange = () => (r.minVolume = +vol.value || 0);
    const cd = el('input', { type: 'number', min: '0', step: '5', value: String(Math.round(r.cooldownMs / 1000)) });
    cd.onchange = () => (r.cooldownMs = Math.max(0, +cd.value) * 1000);
    const tf = el('select');
    tf.append(el('option', { value: '', text: 'каждое событие' }));
    for (const t of TIMEFRAMES) if (t !== 'tick') tf.append(el('option', { value: t, text: `не чаще 1 на бар ${TF_LABEL[t]}` }));
    tf.value = r.tf;
    tf.onchange = () => (r.tf = tf.value as Timeframe | '');
    const snd = el('input', { type: 'checkbox' });
    snd.checked = r.sound;
    snd.onchange = () => (r.sound = snd.checked);
    const ntf = el('input', { type: 'checkbox' });
    ntf.checked = r.notify;
    ntf.onchange = () => (r.notify = ntf.checked);
    const del = el('button', { text: 'Удалить' });
    del.onclick = () => {
      rules = rules.filter((x) => x !== r);
      render();
    };
    return el(
      'div',
      { class: 'card', style: 'margin-bottom:8px' },
      el(
        'div',
        { class: 'form-grid' },
        el('label', {}, en, `Правило #${r.id} включено`),
        el('label', {}, 'Инструмент', sym),
        el('label', {}, 'Типы событий', kinds),
        el('label', {}, 'Мин. score', score),
        el('label', {}, 'Мин. объём', vol),
        el('label', {}, 'Cooldown, с', cd),
        el('label', {}, 'Частота на бар', tf),
        el('label', {}, snd, 'Звук'),
        el('label', {}, ntf, 'Уведомление браузера'),
        del,
      ),
    );
  }

  function renderLog(): void {
    const rows = alerts.recent.map((a) => ({ t: a.t, rule: a.ruleId, ev: a.event })).concat(log.map((l) => ({ t: l.t, rule: l.rule_id, ev: l.body.event })));
    const seen = new Set<string>();
    const uniq = rows.filter((r) => (seen.has(r.rule + r.ev.id) ? false : (seen.add(r.rule + r.ev.id), true)));
    logBox.innerHTML = uniq.length
      ? '<table><thead><tr><th class="l">Время (UTC)</th><th class="l">Инструмент</th><th class="l">Событие</th><th>Цена</th><th>Score</th><th class="l">Почему</th></tr></thead><tbody>' +
        uniq
          .slice(0, 300)
          .map((l) => `<tr><td class="l">${fmtDateTime(l.t)}</td><td class="l">${esc(l.ev.symbol)}</td><td class="l">${esc(l.ev.title)}</td><td>${fmtP(l.ev.price, store.meta?.pricePrecision ?? 2)}</td><td>${l.ev.kind === 'feed' ? '—' : l.ev.confidence}</td><td class="wrap">${esc(l.ev.explain)}</td></tr>`)
          .join('') +
        '</tbody></table>'
      : '<p class="muted">Алертов пока нет.</p>';
  }

  permBtn.onclick = async () => {
    if ('Notification' in window) await Notification.requestPermission();
    alerts.beep(660);
    permBtn.textContent = 'Notification' in window ? `Уведомления: ${Notification.permission}` : 'Уведомления не поддерживаются';
  };
  testBtn.onclick = () => alerts.beep(880);
  addBtn.onclick = () => {
    rules.push({ id: Math.max(0, ...rules.map((r) => r.id)) + 1, enabled: true, symbol: store.symbol, kinds: ['absorption'], minScore: 60, minVolume: 0, tf: '', cooldownMs: 60_000, sound: true, notify: true });
    render();
  };
  saveBtn.onclick = async () => {
    try {
      await api('/api/alerts', {}, { method: 'PUT', body: JSON.stringify(rules) });
      info.textContent = 'Сохранено на сервере.';
    } catch (e) {
      info.textContent = 'Ошибка: ' + (e as Error).message;
    }
  };
  alerts.onChange = () => renderLog();
  return { id: 'alerts', title: 'Алерты', root, show: () => void load(), hide: () => {} };
}
