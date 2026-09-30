// Portfolio: the paper portfolio over the site's strategies with the risk manager (server: services/portfolio.ts).
import type { Tab } from '../main.js';
import { api, el, esc, fmtDateTime, Painter } from '../util.js';

interface Expo {
  symbol: string;
  net: number;
  share: number;
  parts: Record<string, number>;
  overLimit: boolean;
}
interface View {
  status: string;
  error: string;
  params?: { capital: number; trendRisk: number; factorWeight: number; volWindowDays: number; minHistoryDays: number; maxCoinExposure: number };
  startedAt?: number;
  days?: number;
  k?: number;
  kActive?: boolean;
  equity?: number;
  returnPct?: number;
  maxDrawdown?: number;
  ddExpected?: { p50: number; p95: number; p99: number };
  ddVerdict?: string;
  sleeves?: {
    trend: { r: number; trades: number; pnl: number; open: { symbol: string; dir: number; entry: number; stop: number; price: number; model: string }[] };
    carry: { equity: number; pnlPct: number };
    factor: { equity: number; pnlPct: number };
  };
  exposures?: Expo[];
  path?: { day: number; eq: number; k: number }[];
  tracking?: { sleeve: string; note: string; days: number; live: number; annRet: number; expected: number; sd: number; z: number; verdict: string }[];
  journal?: {
    trend: { symbol: string; t: number; closedAt?: number; direction: string; model: string; entry: number; r: number; status: string }[];
    carry: { symbol: string; openedAt: number; closedAt: number; net: number; reason: string }[];
    factorLast: { t: number; long: string[]; short: string[] } | null;
  };
}
const MODEL: Record<string, string> = { TREND_BREAKOUT: 'тренд', BOUNCE: 'отбой', BREAKOUT: 'пробой', FALSE_BREAK: 'ложный пробой' };

const pct = (x: number | undefined, d = 1) => (x === undefined || !Number.isFinite(x) ? '—' : (x * 100).toFixed(d) + '%');
const num = (x: number | undefined, d = 2) => (x === undefined || !Number.isFinite(x) ? '—' : x.toFixed(d));
const cls = (x: number) => (x > 0 ? 'pos' : x < 0 ? 'neg' : '');
const SLEEVE: Record<string, string> = { trend: 'тренд/уровни', factor: 'фактор фандинга' };

function spark(path: { eq: number }[]): string {
  if (path.length < 2) return '';
  const w = 600;
  const h = 80;
  const ys = path.map((p) => p.eq);
  const lo = Math.min(...ys);
  const hi = Math.max(...ys);
  const pts = ys.map((y, i) => `${((i / (ys.length - 1)) * w).toFixed(1)},${(h - ((y - lo) / (hi - lo || 1)) * (h - 4) - 2).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;max-width:${w}px;height:${h}px;display:block"><polyline fill="none" stroke="#26a69a" stroke-width="1.5" points="${pts}"/></svg>`;
}

function journalHtml(j: View['journal']): string {
  if (!j) return '';
  const tr = j.trend
    .map((x) => `<tr><td class="l">${fmtDateTime(x.t)}</td><td class="l">${esc(x.symbol)}</td><td class="l ${x.direction === 'LONG' ? 'pos' : 'neg'}">${esc(x.direction)}</td><td class="l">${esc(MODEL[x.model] ?? x.model)}</td><td>${x.entry}</td><td class="${cls(x.r)}">${x.r >= 0 ? '+' : ''}${x.r.toFixed(2)}R</td><td class="l muted">${x.status === 'open' ? 'открыта' : x.closedAt ? 'закрыта ' + fmtDateTime(x.closedAt) : esc(x.status)}</td></tr>`)
    .join('');
  const ca = j.carry.map((x) => `<tr><td class="l">${esc(x.symbol)}</td><td class="l">${fmtDateTime(x.openedAt)} — ${fmtDateTime(x.closedAt)}</td><td class="${cls(x.net)}">${x.net >= 0 ? '+' : ''}${x.net.toFixed(2)} USDT</td><td class="l muted">${esc(x.reason)}</td></tr>`).join('');
  return (
    `<h3>Журнал сделок</h3>` +
    `<p class="muted"><b>Тренд и уровни</b> (с начала портфеля):</p>` +
    (tr ? `<table><thead><tr><th class="l">Вход</th><th class="l">Монета</th><th class="l">Сторона</th><th class="l">Модель</th><th>Цена</th><th>Результат</th><th class="l">Статус</th></tr></thead><tbody>${tr}</tbody></table>` : '<p class="muted">Сделок пока не было (модели входят редко: ≈ 1–2 раза в месяц на монету).</p>') +
    `<p class="muted"><b>Кэрри</b> — закрытые позиции:</p>` +
    (ca ? `<table><thead><tr><th class="l">Монета</th><th class="l">Период</th><th>Итог</th><th class="l">Причина закрытия</th></tr></thead><tbody>${ca}</tbody></table>` : '<p class="muted">Закрытых позиций пока нет.</p>') +
    (j.factorLast ? `<p class="muted"><b>Фактор фандинга</b> — последняя ребалансировка ${fmtDateTime(j.factorLast.t)}: лонг ${esc(j.factorLast.long.join(', '))}; шорт ${esc(j.factorLast.short.join(', '))}.</p>` : '')
  );
}

export function createPortfolioTab(): Tab {
  const root = el('section', { id: 'tab-portfolio', role: 'tabpanel' });
  const box = el('div', { class: 'scroll' });
  root.append(box);
  let v: View | null = null;
  let err = '';

  async function refresh(): Promise<void> {
    try {
      v = await api<View>('/api/portfolio');
      err = '';
    } catch (e) {
      err = (e as Error).message;
    }
    painter.mark();
  }

  function render(): void {
    if (!v || v.status !== 'ok' || !v.params || !v.sleeves) {
      box.innerHTML = `<p class="muted">${err ? 'нет данных: ' + esc(err) : 'Портфель собирает первые данные (первый снимок — через несколько минут после запуска сервера)…'}</p>`;
      return;
    }
    const p = v.params;
    const s = v.sleeves;
    const expo = v.exposures ?? [];
    box.innerHTML =
      `<h3>Портфель (paper): тренд + кэрри + фактор фандинга с риск-менеджером</h3>` +
      `<p class="muted">Три стратегии со слабой связью между собой (история 06.2021–09.2026: корреляции −0,11…+0,04). Риск-менеджер уменьшает направленную часть (тренд и фактор фандинга), когда её волатильность за ${p.volWindowDays} дней выше обычной: k = min(1, обычная / текущая). На истории: Шарп 1,99 → 2,25, худший месяц −6,4 % → −4,7 %, доходность ≈ +23 % в год при макс. просадке 9,4 %. Кэрри не масштабируется (его колебания малы). Риск тренда — ${pct(p.trendRisk, 2)} капитала на сделку, фактор фандинга — ${pct(p.factorWeight, 0)} своего размера. Лимит на одну монету — ${pct(p.maxCoinExposure, 0)} капитала. Всё виртуально: на биржу ничего не отправляется.</p>` +
      `<table><tbody>` +
      `<tr><td class="l">Капитал</td><td>${num(v.equity)} USDT</td><td class="${cls(v.returnPct ?? 0)}">${(v.returnPct ?? 0) >= 0 ? '+' : ''}${num(v.returnPct)}%</td></tr>` +
      `<tr><td class="l">Макс. просадка</td><td>${pct(v.maxDrawdown)}</td><td class="l muted">с ${fmtDateTime(v.startedAt ?? 0)}, дней истории: ${v.days}</td></tr>` +
      (v.ddExpected ? `<tr><td class="l">Ожидаемая просадка</td><td>${pct(v.ddExpected.p50, 0)} / ${pct(v.ddExpected.p95, 0)} / ${pct(v.ddExpected.p99, 0)}</td><td class="l muted">обычная / в 5 % случаев / в 1 % случаев (Монте-Карло: те же дни истории в случайном порядке, 3 000 раз). Сейчас: ${esc(v.ddVerdict ?? '')}</td></tr>` : '') +
      `<tr><td class="l">Множитель риска k</td><td>${num(v.k)}</td><td class="l muted">${v.kActive ? (v.k! < 1 ? 'рынок нервнее обычного — позиции уменьшены' : 'волатильность в норме — полный размер') : `включится после ${p.minHistoryDays} дней истории (сейчас k = 1)`}</td></tr>` +
      `</tbody></table>` +
      spark(v.path ?? []) +
      `<h3>Части портфеля</h3><table><thead><tr><th class="l">Стратегия</th><th>Результат</th><th class="l">Детали</th></tr></thead><tbody>` +
      `<tr><td class="l">Тренд + уровни Герчика</td><td class="${cls(s.trend.r)}">${s.trend.r >= 0 ? '+' : ''}${num(s.trend.r)}R (${s.trend.pnl >= 0 ? '+' : ''}${num(s.trend.pnl)} USDT)</td><td class="l muted">${s.trend.trades} сделок с начала, открыто ${s.trend.open.length}</td></tr>` +
      `<tr><td class="l">Кэрри</td><td class="${cls(s.carry.pnlPct)}">${num(s.carry.pnlPct)}%</td><td class="l muted">капитал книги ${num(s.carry.equity)} USDT (вкладка «Фандинг»)</td></tr>` +
      `<tr><td class="l">Фактор фандинга</td><td class="${cls(s.factor.pnlPct)}">${num(s.factor.pnlPct)}%</td><td class="l muted">капитал книги ${num(s.factor.equity)} USDT, в портфеле ${pct(p.factorWeight, 0)}</td></tr>` +
      `</tbody></table>` +
      `<h3>Живой результат против бэктеста</h3>` +
      `<p class="muted">Ожидание = годовая доходность бэктеста × прошедшее время; разброс = годовая волатильность × √(время). z — насколько живой результат отличается от ожидания в единицах разброса: |z| < 1 — как на истории, z < −2 — стратегия ведёт себя хуже, чем на истории (повод разобраться).</p>` +
      `<table><thead><tr><th class="l">Часть</th><th>Живой</th><th>Ожидание</th><th>± разброс</th><th>z</th><th class="l">Вывод</th></tr></thead><tbody>` +
      (v.tracking ?? [])
        .map((x) => `<tr><td class="l">${esc(x.note)}</td><td class="${cls(x.live)}">${pct(x.live, 2)}</td><td>${pct(x.expected, 2)}</td><td>${pct(x.sd, 2)}</td><td>${num(x.z)}</td><td class="l ${x.z < -2 && x.days >= 30 ? 'neg' : ''}">${esc(x.verdict)}</td></tr>`)
        .join('') +
      `</tbody></table>` +
      `<h3>Экспозиция по монетам (с учётом k)</h3>` +
      (expo.length
        ? `<table><thead><tr><th class="l">Монета</th><th>Чистая позиция, USDT</th><th>Доля капитала</th><th class="l">Из чего</th><th></th></tr></thead><tbody>` +
          expo
            .map((e) => `<tr><td class="l">${esc(e.symbol)}</td><td class="${cls(e.net)}">${e.net >= 0 ? '+' : ''}${e.net.toFixed(0)}</td><td>${pct(e.share)}</td><td class="l muted">${Object.entries(e.parts).map(([k, x]) => `${esc(SLEEVE[k] ?? k)} ${x >= 0 ? '+' : ''}${x.toFixed(0)}`).join(', ')}</td><td class="l ${e.overLimit ? 'neg' : ''}">${e.overLimit ? 'выше лимита' : ''}</td></tr>`)
            .join('') +
          '</tbody></table>'
        : '<p class="muted">Открытых направленных позиций нет.</p>') +
      journalHtml(v.journal);
  }
  const painter = new Painter(render, 1000);
  let iv = 0;
  return {
    id: 'portfolio',
    title: 'Портфель',
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
