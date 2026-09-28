// News: headlines and exchange announcements classified by the local LLM (or keywords), event-risk flags per coin and
// the forward journal of the flags (server: services/news.ts).
import type { Tab } from '../main.js';
import { api, el, esc, fmtDateTime, Painter } from '../util.js';

interface Item {
  id: string;
  source: string;
  title: string;
  url: string;
  t: number;
  coins: string[];
  event: string;
  sentiment: number;
  severity: number;
  by: string;
}
interface Flag {
  coin: string;
  since: number;
  until: number;
  reason: string;
  r1?: number;
  r3?: number;
}
interface View {
  status: string;
  error: string;
  llm?: { ok: boolean; model: string; error: string; lastAt: number; classified: number };
  sources?: Record<string, { ok: boolean; n: number; error: string; at: number }>;
  known?: number;
  active?: Flag[];
  journal?: { flags: number; lifted?: number; scored1: number; avgR1: number | null; scored3: number; avgR3: number | null; recent: Flag[] };
  items?: Item[];
}

const EVENT: Record<string, string> = { hack: 'взлом', delisting: 'делистинг', unlock: 'разлок токенов', regulatory: 'регулятор', listing: 'листинг', partnership: 'партнёрство', macro: 'макро', other: 'прочее' };
const pct = (x: number | null | undefined) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : (x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%');
const cls = (x: number) => (x > 0 ? 'pos' : x < 0 ? 'neg' : '');

export function createNewsTab(): Tab {
  const root = el('section', { id: 'tab-news', role: 'tabpanel' });
  const box = el('div', { class: 'scroll' });
  root.append(box);
  let v: View | null = null;
  let err = '';

  async function refresh(): Promise<void> {
    try {
      v = await api<View>('/api/news');
      err = '';
    } catch (e) {
      err = (e as Error).message;
    }
    painter.mark();
  }

  function render(): void {
    if (!v || v.status !== 'ok') {
      box.innerHTML = `<p class="muted">${err ? 'нет данных: ' + esc(err) : 'Сбор новостей начнётся через минуту после запуска сервера…'}</p>`;
      return;
    }
    const j = v.journal!;
    const src = Object.entries(v.sources ?? {})
      .map(([k, s]) => `${esc(k)} ${s.ok ? `✓ ${s.n}` : `✗ ${esc(s.error)}`}`)
      .join(' · ');
    box.innerHTML =
      `<h3>Новости и объявления бирж → фильтр событийного риска</h3>` +
      `<p class="muted">Заголовки крупных крипто-СМИ и объявления Bybit и Binance о делистингах раз в 5 минут размечает локальная языковая модель на сервере (бесплатно; если она недоступна — правила по ключевым словам): монеты, тип события, тон и серьёзность. Серьёзное негативное событие (взлом, делистинг, иск регулятора, крупный разлок) ставит монете флаг на 72 часа: тренд не открывает лонг, кэрри закрывает позицию, фактор фандинга её исключает. Польза проверяется только вперёд — журнал ниже сравнивает помеченные монеты с BTC через 1 и 3 дня (отрицательные значения = фильтр уберёг от падения).</p>` +
      `<p class="muted">Модель: ${v.llm?.ok ? `✓ ${esc(v.llm.model)}, размечено ${v.llm.classified}` : `✗ недоступна${v.llm?.error ? ' (' + esc(v.llm.error) + ')' : ''} — работают правила по ключевым словам`} · источники: ${src || '—'} · монет в словаре: ${v.known ?? 0}</p>` +
      `<h3>Активные флаги (${v.active?.length ?? 0})</h3>` +
      (v.active?.length
        ? `<table><thead><tr><th class="l">Монета</th><th class="l">С</th><th class="l">До</th><th class="l">Причина</th></tr></thead><tbody>` +
          v.active.map((f) => `<tr><td class="l neg">${esc(f.coin)}</td><td class="l">${fmtDateTime(f.since)}</td><td class="l">${fmtDateTime(f.until)}</td><td class="l">${esc(f.reason)}</td></tr>`).join('') +
          '</tbody></table>'
        : '<p class="muted">Сейчас ни одна монета не под флагом.</p>') +
      `<h3>Журнал проверки вперёд</h3>` +
      `<p>Флагов всего: ${j.flags}${j.lifted ? ` (ещё ${j.lifted} снято: модель перечитала заголовок и не подтвердила риск)` : ''}. Монета минус BTC через 1 день: ${pct(j.avgR1)} (n=${j.scored1}); через 3 дня: ${pct(j.avgR3)} (n=${j.scored3}).</p>` +
      (j.recent.length
        ? `<table><thead><tr><th class="l">Монета</th><th class="l">Флаг</th><th>+1 день</th><th>+3 дня</th><th class="l">Причина</th></tr></thead><tbody>` +
          j.recent.map((f) => `<tr><td class="l">${esc(f.coin)}</td><td class="l">${fmtDateTime(f.since)}</td><td class="${cls(f.r1 ?? 0)}">${pct(f.r1)}</td><td class="${cls(f.r3 ?? 0)}">${pct(f.r3)}</td><td class="l muted">${esc(f.reason)}</td></tr>`).join('') +
          '</tbody></table>'
        : '') +
      `<h3>Последние новости</h3><table><thead><tr><th class="l">Время</th><th class="l">Источник</th><th class="l">Заголовок</th><th class="l">Монеты</th><th class="l">Событие</th><th>Тон</th><th>Серьёзность</th></tr></thead><tbody>` +
      (v.items ?? [])
        .map(
          (it) =>
            `<tr><td class="l">${fmtDateTime(it.t)}</td><td class="l">${esc(it.source)}</td><td class="l">${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a>` : esc(it.title)}</td><td class="l">${esc(it.coins.join(', '))}</td><td class="l">${esc(EVENT[it.event] ?? it.event)}${it.by === 'keywords' ? ' <span class="muted">(правила)</span>' : ''}</td><td class="${cls(it.sentiment)}">${it.sentiment > 0 ? '+' : ''}${it.sentiment}</td><td>${it.severity}</td></tr>`,
        )
        .join('') +
      '</tbody></table>';
  }
  const painter = new Painter(render, 1000);
  let iv = 0;
  return {
    id: 'news',
    title: 'Новости',
    root,
    show: () => {
      void refresh();
      iv = window.setInterval(() => void refresh(), 60_000);
      painter.show();
    },
    hide: () => {
      clearInterval(iv);
      painter.hide();
    },
  };
}
