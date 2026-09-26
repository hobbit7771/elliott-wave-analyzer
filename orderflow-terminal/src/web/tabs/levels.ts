// Levels: real-time watchlist of strong D1 levels (nearest support / resistance, distance, state) and the
// honest backtest of level setups for the current instrument.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, esc, fmtP, fmtDateTime, ownerToken, Painter } from '../util.js';

interface Side {
  price: number;
  strength: number;
  status: string;
  state: string;
  distPct: number;
  distAtr: number;
}
interface Row {
  key: string;
  symbol?: string;
  price?: number;
  support?: Side | null;
  resistance?: Side | null;
  status?: string;
  error?: string;
}
interface St {
  total: number;
  long: number;
  short: number;
  closed: number;
  winRate: number;
  lossRate: number;
  avgR: number;
  medianR: number;
  profitFactor: number | null;
  expectancy: number;
  maxLosingStreak: number;
  avgMaeR: number;
  avgMfeR: number;
}

const n2 = (x: number | null | undefined, d = 2) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : x.toFixed(d));
const pc = (x: number) => (Number.isFinite(x) ? (x * 100).toFixed(0) + '%' : '—');

function statsRow(name: string, s: St): string {
  return `<tr><td class="l">${esc(name)}</td><td>${s.total}</td><td>${s.long}/${s.short}</td><td>${s.closed}</td><td>${pc(s.winRate)}</td><td>${n2(s.avgR)}</td><td>${n2(s.medianR)}</td><td>${s.profitFactor === null ? '∞' : n2(s.profitFactor)}</td><td>${s.maxLosingStreak}</td><td>${n2(s.avgMfeR)}</td><td>${n2(s.avgMaeR)}</td></tr>`;
}
const STATS_HEAD = '<tr><th class="l">Выборка</th><th>Сетапов</th><th>L/S</th><th>Закрыто</th><th>Win</th><th>Avg R</th><th>Median R</th><th>PF</th><th>Макс. серия −</th><th>MFE R</th><th>MAE R</th></tr>';

export function createLevelsTab(): Tab {
  const root = el('section', { id: 'tab-levels', role: 'tabpanel' });
  const addIn = el('input', { type: 'text', placeholder: 'например INJUSDT', 'aria-label': 'Добавить монету' });
  const addBtn = el('button', { text: 'Добавить' });
  const runBtn = el('button', { text: 'Пересчитать историю', title: 'Только владелец: заново прогнать бэктест для текущего инструмента' });
  const note = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, el('b', { text: 'Watchlist' }), addIn, addBtn, runBtn, note));
  const box = el('div', { class: 'scroll' });
  root.append(box);
  let rows: Row[] = [];
  let keys: string[] = [];
  let hist: { history: Record<string, unknown> | null; live: unknown[]; status: string; error: string } | null = null;

  async function refresh(): Promise<void> {
    try {
      const w = await api<{ keys: string[]; rows: Row[] }>('/api/watchlist');
      keys = w.keys;
      rows = w.rows;
      hist = await api('/api/setups', { source: store.source, symbol: store.symbol });
    } catch (e) {
      note.textContent = 'нет данных: ' + (e as Error).message;
    }
    painter.mark();
  }

  function side(s: Side | null | undefined, kind: string): string {
    if (!s) return `<td class="l muted">нет ${kind}</td><td></td><td></td><td></td><td></td>`;
    return `<td class="l">${kind} ${fmtP(s.price, 6).replace(/\.?0+$/, '')}</td><td>${n2(s.distPct)}%</td><td>${n2(s.distAtr)} ATR</td><td>${s.strength}</td><td class="l state-${esc(s.state)}">${esc(s.state)}${s.status !== 'STRONG' ? ' · ' + esc(s.status) : ''}</td>`;
  }

  function render(): void {
    const watch =
      `<table class="watch"><thead><tr><th class="l">Symbol</th><th>Price</th><th class="l">Ближайшая поддержка</th><th>Dist %</th><th>Dist ATR</th><th>Strength</th><th class="l">State</th><th class="l">Ближайшее сопротивление</th><th>Dist %</th><th>Dist ATR</th><th>Strength</th><th class="l">State</th><th></th></tr></thead><tbody>` +
      rows
        .map(
          (r) =>
            `<tr><td class="l"><a href="#chart" data-open="${esc(r.key)}">${esc(r.symbol ?? r.key)}</a></td><td>${r.price !== undefined && Number.isFinite(r.price) ? fmtP(r.price, 6).replace(/\.?0+$/, '') : '…'}</td>${side(r.support, 'D1 support')}${side(r.resistance, 'D1 resistance')}<td>${ownerToken() ? `<button data-del="${esc(r.key)}">×</button>` : ''}</td></tr>` +
            (r.error ? `<tr><td></td><td colspan="12" class="l muted">${esc(r.status ?? '')}: ${esc(r.error)}</td></tr>` : ''),
        )
        .join('') +
      '</tbody></table>';
    let bt = '';
    const h = hist?.history as
      | { coverage: { from: number; to: number; bars: number; days: number }; chosenBy: string; params: Record<string, unknown>; segments: { name: string; stats: St }[]; overall: St; byDirection: Record<string, St>; byScore: Record<string, St>; byRegime: Record<string, St>; setups: { t: number; direction: string; level: number; levelStrength: number; setupQuality: number; outcome: { status: string; r: number }; segment?: string }[]; computedAt: number; ms: number }
      | null
      | undefined;
    if (h) {
      const recent = [...h.setups].reverse().slice(0, 40);
      bt =
        `<h3>Бэктест сетапов ${esc(store.symbol)} (свеча за свечой, без заглядывания в будущее)</h3>` +
        `<p class="muted">История 15m: ${fmtDateTime(h.coverage.from)} — ${fmtDateTime(h.coverage.to)} (${h.coverage.bars} баров), до неё ${h.coverage.days} дневных свечей для уровней. ${esc(h.chosenBy)} Пересчитано ${fmtDateTime(h.computedAt)} за ${(h.ms / 1000).toFixed(1)} с. Результаты показаны как есть; TRAIN / VALIDATION / OOS — первые 6, следующие 3 и последние 3 месяца, чтобы было видно, насколько результат стабилен во времени.</p>` +
        `<table><thead>${STATS_HEAD}</thead><tbody>${h.segments.map((s) => statsRow(s.name, s.stats)).join('')}${statsRow('ВСЕГО', h.overall)}</tbody></table>` +
        `<h4>По направлению</h4><table><thead>${STATS_HEAD}</thead><tbody>${Object.entries(h.byDirection).map(([k, v]) => statsRow(k, v)).join('')}</tbody></table>` +
        `<h4>По SetupQuality</h4><table><thead>${STATS_HEAD}</thead><tbody>${Object.entries(h.byScore).map(([k, v]) => statsRow(k, v)).join('')}</tbody></table>` +
        `<h4>По режиму рынка (D1)</h4><table><thead>${STATS_HEAD}</thead><tbody>${Object.entries(h.byRegime).map(([k, v]) => statsRow(k, v)).join('')}</tbody></table>` +
        `<h4>Последние сетапы</h4><table><thead><tr><th class="l">Время</th><th>Сторона</th><th>Уровень</th><th>Сила</th><th>Качество</th><th class="l">Результат</th><th>R</th><th class="l">Выборка</th></tr></thead><tbody>` +
        recent.map((s) => `<tr class="${s.direction === 'LONG' ? 'buy' : 'sell'}"><td class="l">${fmtDateTime(s.t)}</td><td>✅ ${s.direction}</td><td>${fmtP(s.level, 6).replace(/\.?0+$/, '')}</td><td>${s.levelStrength}</td><td>${s.setupQuality}</td><td class="l">${esc(s.outcome.status)}</td><td>${n2(s.outcome.r)}</td><td class="l">${esc(s.segment ?? '')}</td></tr>`).join('') +
        '</tbody></table>' +
        `<details><summary>Параметры</summary><pre>${esc(JSON.stringify(h.params, null, 1))}</pre></details>`;
    } else bt = `<p class="muted">Бэктест для ${esc(store.symbol)}: ${esc(hist?.status ?? 'загрузка…')}${hist?.error ? ' — ' + esc(hist.error) : ''}. Первый расчёт скачивает год 15m-свечей и занимает несколько минут.</p>`;
    box.innerHTML = `<div class="pad">${watch}<p class="muted">Цена — последняя сделка биржи (обновление раз в минуту); уровни — сильные D1 (статус STRONG/FLIPPED, сила ≥ порога). State — состояние автомата: WATCHING, APPROACHING, COMPRESSION, TOUCH, REJECTION, CONFIRMED.</p>${bt}</div>`;
    for (const a of box.querySelectorAll<HTMLAnchorElement>('a[data-open]'))
      a.onclick = () => {
        const [source, symbol] = a.dataset.open!.split(':');
        (window as unknown as { oftOpen?: (s: string, y: string) => void }).oftOpen?.(source, symbol);
      };
    for (const b of box.querySelectorAll<HTMLButtonElement>('button[data-del]'))
      b.onclick = async () => {
        const k = keys.filter((x) => x !== b.dataset.del);
        await api('/api/watchlist', {}, { method: 'PUT', body: JSON.stringify({ keys: k }) }).catch((e) => alert((e as Error).message));
        void refresh();
      };
  }
  const painter = new Painter(render, 1000);
  addBtn.onclick = async () => {
    const sym = addIn.value.trim().toUpperCase();
    if (!/^[A-Z0-9]{2,20}$/.test(sym)) return;
    if (!ownerToken()) return void alert('Изменять watchlist может только владелец: войдите на вкладке «Источники».');
    const k = [...new Set([...keys, `${store.source}:${sym}`])];
    try {
      await api('/api/watchlist', {}, { method: 'PUT', body: JSON.stringify({ keys: k }) });
      addIn.value = '';
      note.textContent = `${sym}: загрузка уровней и истории…`;
    } catch (e) {
      alert((e as Error).message);
    }
    void refresh();
  };
  runBtn.onclick = async () => {
    try {
      await api('/api/setups/run', { source: store.source, symbol: store.symbol }, { method: 'POST' });
      note.textContent = 'пересчёт поставлен в очередь';
    } catch (e) {
      alert((e as Error).message);
    }
  };
  let iv = 0;
  return {
    id: 'levels',
    title: 'Уровни',
    root,
    show: () => {
      void refresh();
      iv = window.setInterval(() => void refresh(), 30_000);
      painter.show();
    },
    hide: () => {
      clearInterval(iv);
      painter.hide();
    },
  };
}
