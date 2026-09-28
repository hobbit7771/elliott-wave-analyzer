// Carry: the paper funding-carry strategy (long spot + short perpetual on Bybit, market-neutral) run by the server.
import type { Tab } from '../main.js';
import { api, el, esc, fmtDateTime, Painter } from '../util.js';

interface Row {
  symbol: string;
  spot: number;
  perp: number;
  basisBp: number;
  fundingApr: number;
  apr7: number | null;
  turnover: number;
  held: boolean;
}
interface Pos {
  symbol: string;
  openedAt: number;
  notional: number;
  aprAtEntry: number;
  funding: number;
  fees: number;
  basis: number;
  net: number;
  apr7: number | null;
}
interface Closed extends Pos {
  closedAt: number;
  reason: string;
}
interface Factor {
  params: { lookbackDays: number; quantile: number; rebalanceDays: number; gross: number; feePerSide: number; capital: number };
  startedAt: number;
  lastRebalance: number;
  equity: number;
  last: { t: number; long: string[]; short: string[] } | null;
  positions: { symbol: string; qty: number; notional: number; funding: number; fees: number; fundingDay: number | null }[];
}
interface View {
  status: string;
  factor?: Factor | null;
  error: string;
  params?: { entryApr: number; exitApr: number; slots: number; lookbackDays: number; feePerSide: number; capital: number; marginFrac: number };
  startedAt?: number;
  lastDecisionDay?: number;
  totals?: { realized: number; funding: number; basis: number; fees: number; unrealized: number; equity: number; returnPct: number };
  positions?: Pos[];
  closed?: Closed[];
  rows?: Row[];
}

const pct = (x: number | null | undefined, d = 1) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : (x * 100).toFixed(d) + '%');
const usd = (x: number) => (Number.isFinite(x) ? (x >= 0 ? '+' : '') + x.toFixed(2) : '—');
const px = (x: number) => (Number.isFinite(x) ? String(+x.toPrecision(6)) : '—');
const cls = (x: number) => (x > 0 ? 'pos' : x < 0 ? 'neg' : '');

export function createCarryTab(): Tab {
  const root = el('section', { id: 'tab-carry', role: 'tabpanel' });
  const box = el('div', { class: 'scroll' });
  root.append(box);
  let v: View | null = null;
  let err = '';

  async function refresh(): Promise<void> {
    try {
      v = await api<View>('/api/carry');
      err = '';
    } catch (e) {
      err = (e as Error).message;
    }
    painter.mark();
  }

  function render(): void {
    if (!v || v.status !== 'ok' || !v.params || !v.totals) {
      box.innerHTML = `<p class="muted">${err ? 'нет данных: ' + esc(err) : 'Стратегия загружает котировки и историю фандинга (первое обновление — через 1–2 минуты после запуска сервера)…'}${v?.error ? ' ' + esc(v.error) : ''}</p>`;
      return;
    }
    const p = v.params;
    const t = v.totals;
    const slot = p.capital / (1 + p.marginFrac) / p.slots;
    box.innerHTML =
      `<p class="muted">Портфель из трёх некоррелированных стратегий (история 06.2021–09.2026, корреляции −0,11…+0,04): тренд (вкладка «Уровни», риск 0,25 % на сделку) + кэрри + половина фактора фандинга — ≈ +25,6 % в год, Шарп 1,99, макс. просадка 9,4 % (только тренд: +10,9 %, Шарп 1,09, просадка 12,3 %). Все стратегии здесь — paper: на биржу ничего не отправляется.</p>` +
      `<h3>Кэрри на фандинге (paper): лонг спот + шорт бессрочный фьючерс, Bybit</h3>` +
      `<p class="muted">Позиция не зависит от цены монеты: спот и фьючерс гасят друг друга, а шорт получает фандинг, который платят лонги. ` +
      `Раз в сутки (после 00:05 UTC) сервер закрывает монеты, где средний фандинг за ${p.lookbackDays} дней упал ниже ${pct(p.exitApr, 0)} годовых, и открывает монеты с самым высоким фандингом выше ${pct(p.entryApr, 0)} годовых — до ${p.slots} позиций по ${slot.toFixed(0)} USDT (виртуальный капитал ${p.capital} USDT: спот покупается целиком, под шорт — маржа ${pct(p.marginFrac, 0)}). ` +
      `Комиссии ${pct(p.feePerSide, 2)} на вход и выход (обе ноги, тейкер). Ничего не отправляется на биржу.</p>` +
      `<p class="muted">Проверка на истории (35 монет Binance, 2020–2026, реальный фандинг и цены спота и фьючерса, комиссии): ≈ 9–10 % годовых на капитал при максимальной просадке ≈ 1 %, но доход зависит от режима рынка: 2021 — ≈ +30 %, 2024 — ≈ +9 %, 2022 и 2025–26 — около нуля (фандинг низкий). Вместе с трендовой моделью на том же капитале — Шарп 1,55 вместо 1,09 и меньшая просадка.</p>` +
      `<table><tbody>` +
      `<tr><td class="l">Капитал сейчас</td><td>${t.equity.toFixed(2)} USDT</td><td class="${cls(t.returnPct)}">${t.returnPct >= 0 ? '+' : ''}${t.returnPct.toFixed(2)}%</td></tr>` +
      `<tr><td class="l">Получено фандинга</td><td class="${cls(t.funding)}">${usd(t.funding)}</td><td class="l muted">изменение спреда спот–фьючерс ${usd(t.basis)}, комиссии −${t.fees.toFixed(2)}</td></tr>` +
      `<tr><td class="l">Закрытые / открытые</td><td>${usd(t.realized)}</td><td>${usd(t.unrealized)}</td></tr>` +
      `<tr><td class="l">Запущено</td><td colspan="2" class="l">${fmtDateTime(v.startedAt ?? 0)}; последнее решение ${v.lastDecisionDay ? fmtDateTime(v.lastDecisionDay) : 'ещё не было'}</td></tr>` +
      `</tbody></table>` +
      `<h3>Открытые позиции (${v.positions?.length ?? 0} из ${p.slots})</h3>` +
      (v.positions?.length
        ? `<table><thead><tr><th class="l">Монета</th><th class="l">Открыта</th><th>Объём</th><th>Фандинг при входе</th><th>Фандинг 7 д</th><th>Получено</th><th>Спред</th><th>Комиссии</th><th>Итог</th></tr></thead><tbody>` +
          v.positions.map((x) => `<tr><td class="l">${esc(x.symbol)}</td><td class="l">${fmtDateTime(x.openedAt)}</td><td>${x.notional.toFixed(0)}</td><td>${pct(x.aprAtEntry)}</td><td>${pct(x.apr7)}</td><td class="${cls(x.funding)}">${usd(x.funding)}</td><td class="${cls(x.basis)}">${usd(x.basis)}</td><td>−${x.fees.toFixed(2)}</td><td class="${cls(x.net)}">${usd(x.net)}</td></tr>`).join('') +
          '</tbody></table>'
        : `<p class="muted">Сейчас нет монет с фандингом выше ${pct(p.entryApr, 0)} годовых за ${p.lookbackDays} дней — капитал ждёт (так было, например, почти весь 2022 год и 2025–26 годы).</p>`) +
      `<h3>Фандинг по ликвидным монетам (спот и фьючерс на Bybit)</h3>` +
      `<table><thead><tr><th class="l">Монета</th><th>Спот</th><th>Фьючерс</th><th>Спред, б.п.</th><th>Текущий фандинг, годовых</th><th>Средний за 7 д, годовых</th><th>Оборот 24 ч, млн</th><th></th></tr></thead><tbody>` +
      (v.rows ?? [])
        .map((r) => `<tr><td class="l">${esc(r.symbol)}</td><td>${px(r.spot)}</td><td>${px(r.perp)}</td><td>${r.basisBp.toFixed(1)}</td><td class="${cls(r.fundingApr)}">${pct(r.fundingApr)}</td><td class="${cls(r.apr7 ?? 0)}">${pct(r.apr7)}</td><td>${(r.turnover / 1e6).toFixed(0)}</td><td class="l">${r.held ? '● в позиции' : (r.apr7 ?? -1) > p.entryApr ? 'кандидат' : ''}</td></tr>`)
        .join('') +
      '</tbody></table>' +
      factorHtml(v.factor) +
      (v.closed?.length
        ? `<h3>Закрытые (кэрри)</h3><table><thead><tr><th class="l">Монета</th><th class="l">Период</th><th>Фандинг</th><th>Спред</th><th>Комиссии</th><th>Итог</th><th class="l">Причина</th></tr></thead><tbody>` +
          v.closed.map((x) => `<tr><td class="l">${esc(x.symbol)}</td><td class="l">${fmtDateTime(x.openedAt)} — ${fmtDateTime(x.closedAt)}</td><td>${usd(x.funding)}</td><td>${usd(x.basis)}</td><td>−${x.fees.toFixed(2)}</td><td class="${cls(x.net)}">${usd(x.net)}</td><td class="l">${esc(x.reason)}</td></tr>`).join('') +
          '</tbody></table>'
        : '');
  }
  function factorHtml(f: Factor | null | undefined): string {
    if (!f) return '';
    const p = f.params;
    const ret = ((f.equity - p.capital) / p.capital) * 100;
    const pos = [...f.positions].sort((a, b) => b.notional - a.notional);
    return (
      `<h3>Фактор фандинга (paper): лонг монет с низким фандингом / шорт монет с высоким</h3>` +
      `<p class="muted">Раз в ${p.rebalanceDays} дней сервер ранжирует ликвидные фьючерсы по среднему фандингу за ${p.lookbackDays} дней: треть с самым низким — в лонг, треть с самым высоким — в шорт, размер обратно пропорционален волатильности, по ${pct(p.gross, 0)} капитала на сторону (рыночно-нейтрально). Высокий фандинг = толпа в лонгах; такие монеты дальше растут слабее, а шорт ещё и получает фандинг. Проверка на истории (35 фьючерсов Binance, 2021–2026, комиссии): ≈ +29 % в год, Шарп 1,48, просадка 24 %, связь с BTC ≈ 0; без любой одной монеты Шарп ≥ 1,03. Виртуальный капитал ${p.capital} USDT, комиссия ${pct(p.feePerSide, 3)} за сторону.</p>` +
      `<table><tbody><tr><td class="l">Капитал сейчас</td><td>${f.equity.toFixed(2)} USDT</td><td class="${cls(ret)}">${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%</td></tr>` +
      `<tr><td class="l">Запущено / последняя ребалансировка</td><td colspan="2" class="l">${fmtDateTime(f.startedAt)} / ${f.lastRebalance ? fmtDateTime(f.lastRebalance) : 'ещё не было'}</td></tr></tbody></table>` +
      (pos.length
        ? `<table><thead><tr><th class="l">Монета</th><th class="l">Сторона</th><th>Объём, USDT</th><th>Фандинг 7 д, годовых</th><th>Получено фандинга</th><th>Комиссии</th></tr></thead><tbody>` +
          pos.map((x) => `<tr><td class="l">${esc(x.symbol)}</td><td class="l ${x.qty > 0 ? 'pos' : 'neg'}">${x.qty > 0 ? 'LONG' : 'SHORT'}</td><td>${Math.abs(x.notional).toFixed(0)}</td><td>${pct(x.fundingDay === null ? null : x.fundingDay * 365)}</td><td class="${cls(x.funding)}">${usd(x.funding)}</td><td>−${x.fees.toFixed(2)}</td></tr>`).join('') +
          '</tbody></table>'
        : '<p class="muted">Первая ребалансировка — после 00:05 UTC, когда будет история фандинга и волатильности (нужно ≥ 10 монет).</p>')
    );
  }
  const painter = new Painter(render, 1000);
  let iv = 0;
  return {
    id: 'carry',
    title: 'Фандинг',
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
