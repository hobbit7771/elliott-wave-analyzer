// Signals: journal of detector events (with confidence + explanation), large limit orders with full
// lifecycle fields, liquidity clusters and current iceberg candidates.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { el, esc, fmtQ, fmtP, fmtTime, fmtDateTime, confColor, KIND_LABEL, Painter } from '../util.js';
import type { EventKind } from '../../core/types.js';

export function createSignalsTab(): Tab {
  const root = el('section', { id: 'tab-signals', role: 'tabpanel' });
  const kindSel = el('select', { 'aria-label': 'Тип события' });
  kindSel.append(el('option', { value: '', text: 'Все типы' }));
  for (const [k, l] of Object.entries(KIND_LABEL)) kindSel.append(el('option', { value: k, text: l }));
  const confIn = el('input', { type: 'number', min: '0', max: '100', step: '5', value: '40', title: 'Минимальный score' });
  const sideSel = el('select', { 'aria-label': 'Side' });
  for (const [v, l] of [['', 'Обе стороны'], ['bull', 'Bid / покупки'], ['bear', 'Ask / продажи']]) sideSel.append(el('option', { value: v, text: l }));
  const viewSel = el('select', { 'aria-label': 'Table' });
  for (const [v, l] of [['events', 'Журнал событий'], ['large', 'Крупные уровни видимой ликвидности'], ['clusters', 'Кластеры ликвидности'], ['ice', 'Кандидаты в айсберги']]) viewSel.append(el('option', { value: v, text: l }));
  const exportBtn = el('button', { text: 'Экспорт JSON' });
  const count = el('span', { class: 'muted' });
  root.append(el('div', { class: 'toolbar' }, viewSel, kindSel, el('label', {}, 'Мин. score', confIn), sideSel, exportBtn, count));
  const disclaimer = el('div', { class: 'pad muted', text: 'Предполагаемый айсберг, признаки пополнения и поглощение — выводы по публичным сделкам и L2-стакану, а не факт. Скрытый остаток заявки не виден; L2 Binance показывает суммарный объём на цене, а не отдельные ордера (нет order ID, владельца и очереди). Score 0–100 — эвристика, не калиброванная вероятность.' });
  const box = el('div', { class: 'scroll' });
  root.append(disclaimer, box);
  const painter = new Painter(render, 500);

  function render(): void {
    const v = viewSel.value;
    kindSel.disabled = v !== 'events';
    const minC = +confIn.value || 0;
    const d = store.meta?.pricePrecision ?? 2;
    if (v === 'events') {
      const k = kindSel.value as EventKind | '';
      const side = sideSel.value;
      const list = store
        .eventList()
        .filter((e) => (!k || e.kind === k) && (e.kind === 'feed' || e.confidence >= minC))
        .filter((e) => !side || (side === 'bull' ? e.side === 'bid' || e.side === 'buy' : e.side === 'ask' || e.side === 'sell'))
        .reverse()
        .slice(0, 500);
      count.textContent = `показано ${list.length}`;
      box.innerHTML =
        '<table><thead><tr><th class="l">Время (UTC)</th><th class="l">Событие</th><th>Сторона</th><th>Цена</th><th>Score</th><th class="l">Статус</th><th class="l">Почему</th></tr></thead><tbody>' +
        list
          .map(
            (e) =>
              `<tr class="${e.side === 'bid' || e.side === 'buy' ? 'buy' : e.side ? 'sell' : ''}"><td class="l">${fmtDateTime(e.t)}</td><td class="l">${esc(e.title)}</td><td class="side">${e.side ?? ''}</td><td>${fmtP(e.price, d)}${e.priceHi !== undefined && e.priceHi !== e.price ? '–' + fmtP(e.priceHi, d) : ''}</td><td><span class="conf" style="background:${confColor(e.confidence)}">${e.confidence}</span></td><td class="l">${esc(e.status ?? '')}</td><td class="wrap">${esc(e.explain)}</td></tr>`,
          )
          .join('') +
        '</tbody></table>';
    } else if (v === 'large') {
      const list = store.large.list.filter((x) => x.confidence >= minC);
      const thr = store.large.thr;
      count.textContent = thr ? (thr.warm ? `порог bid ${fmtQ(thr.bid)} / ask ${fmtQ(thr.ask)} (ATR-множитель ${thr.atrFactor.toFixed(2)})` : `прогрев: ${thr.samples} выборок уровней`) : '';
      box.innerHTML =
        '<table><thead><tr><th>Цена</th><th>Сторона</th><th>Объём</th><th>Пик</th><th class="l">Появился</th><th>Держится</th><th>Пополнений</th><th>Исполнено (оценка)</th><th>Снято (оценка)</th><th class="l">Статус</th><th>Score</th><th class="l">Источник</th></tr></thead><tbody>' +
        list
          .map(
            (x) =>
              `<tr class="${x.side === 'bid' ? 'buy' : 'sell'}"><td>${fmtP(x.price, d)}</td><td class="side">${x.side}</td><td>${fmtQ(x.size)}</td><td>${fmtQ(x.peak)}</td><td class="l">${fmtTime(x.firstSeen)}</td><td>${(x.holdMs / 1000).toFixed(0)}s</td><td>${x.replenishments}</td><td>${fmtQ(x.executed)}</td><td>${fmtQ(x.cancelled)}</td><td class="l">${x.status.replace('_', ' ')}</td><td><span class="conf" style="background:${confColor(x.confidence)}">${x.confidence}</span></td><td class="l">${x.source}</td></tr>`,
          )
          .join('') +
        '</tbody></table>' +
        (list.length ? '' : '<p class="pad muted">Сейчас нет уровней выше динамического порога.</p>');
    } else if (v === 'clusters') {
      const list = store.clusters.list.filter((c) => c.confidence >= minC);
      count.textContent = `кластеров ${list.length}, зон вакуума ${store.clusters.vacuums.length}`;
      box.innerHTML =
        '<table><thead><tr><th class="l">Тип</th><th>Сторона</th><th>От</th><th>До</th><th>Всего</th><th>Уровней</th><th>Плотность/тик</th><th>Исполнено</th><th class="l">Since</th><th>Conf</th></tr></thead><tbody>' +
        list
          .map(
            (c) =>
              `<tr class="${c.side === 'bid' ? 'buy' : 'sell'}"><td class="l">${esc(c.label)}</td><td class="side">${c.side}</td><td>${fmtP(c.lo, d)}</td><td>${fmtP(c.hi, d)}</td><td>${fmtQ(c.total)}</td><td>${c.levels}</td><td>${fmtQ(c.density)}</td><td>${fmtQ(c.executed)}</td><td class="l">${fmtTime(c.firstSeen)}</td><td><span class="conf" style="background:${confColor(c.confidence)}">${c.confidence}</span></td></tr>`,
          )
          .join('') +
        '</tbody></table>';
    } else {
      const list = [...store.ice].sort((a, b) => b.confidence - a.confidence);
      count.textContent = `уровней под наблюдением: ${list.length}`;
      box.innerHTML =
        '<table><thead><tr><th>Цена</th><th>Сторона</th><th>Сверх видимого (предп.)</th><th>Пополнений</th><th>Score</th><th class="l">Достаточно данных</th></tr></thead><tbody>' +
        list
          .map((c) => `<tr class="${c.side === 'bid' ? 'buy' : 'sell'}"><td>${fmtP(c.price, d)}</td><td class="side">${c.side}</td><td>${fmtQ(c.hidden)}</td><td>${c.refills}</td><td><span class="conf" style="background:${confColor(c.confidence)}">${c.confidence}</span></td><td class="l">${c.eligible ? 'да' : 'нет, данных недостаточно'}</td></tr>`)
          .join('') +
        '</tbody></table>';
    }
  }
  for (const i of [kindSel, confIn, sideSel, viewSel]) i.addEventListener('change', () => painter.mark());
  exportBtn.onclick = () => {
    const q = new URLSearchParams({ source: store.source, symbol: store.symbol });
    window.open('/api/export/events.json?' + q.toString(), '_blank');
  };
  for (const t of ['events', 'large', 'clusters', 'ice', 'reset'] as const) store.on(t, () => painter.mark());
  return { id: 'signals', title: 'Сигналы', root, show: () => painter.show(), hide: () => painter.hide() };
}
