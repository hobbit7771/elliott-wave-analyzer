// Источники, диагностика, вход владельца, список записи, настройки детекторов, политика хранения.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, esc, fmtDateTime, ownerToken } from '../util.js';

interface SourcesResp {
  available: { id: string; name: string; caps: Record<string, unknown>; limitations: string[]; status: string }[];
  unavailable: { id: string; name: string; reason: string; status?: string; needs?: string[] }[];
}

const mb = (b: number) => (b / 1048576).toFixed(1) + ' МБ';

export function createSourcesTab(): Tab {
  const root = el('section', { id: 'tab-sources', role: 'tabpanel' });
  const body = el('div', { class: 'scroll pad' });
  root.append(body);
  const ownerBox = el('div');
  const diagBox = el('div');
  const recBox = el('div');
  const srcBox = el('div');
  const cfgBox = el('div');
  body.append(el('h3', { text: 'Доступ владельца' }), ownerBox, el('h3', { text: 'Диагностика' }), diagBox, el('h3', { text: 'Записываемые инструменты' }), recBox, el('h3', { text: 'Источники данных' }), srcBox, el('h3', { text: 'Настройки детекторов (текущий инструмент)' }), cfgBox);

  // ---------- owner ----------
  function renderOwner(): void {
    const inp = el('input', { type: 'password', placeholder: 'OWNER_TOKEN из настроек Render', autocomplete: 'off', style: 'width:260px' });
    const save = el('button', { text: 'Войти' });
    const out = el('button', { text: 'Выйти' });
    const st = el('span', { class: 'muted', text: ownerToken() ? 'Токен сохранён в этом браузере.' : 'Без токена доступны только просмотр рынка и истории.' });
    save.onclick = async () => {
      localStorage.setItem('oft:owner', inp.value.trim());
      try {
        await api('/api/auth/check');
        st.textContent = 'Токен принят сервером.';
      } catch (e) {
        localStorage.removeItem('oft:owner');
        st.textContent = 'Отклонено: ' + (e as Error).message;
      }
    };
    out.onclick = () => {
      localStorage.removeItem('oft:owner');
      st.textContent = 'Токен удалён из браузера.';
    };
    ownerBox.replaceChildren(
      el('p', { class: 'muted', text: 'Изменение настроек, списка записи, paper, алертов, рисунков и удаление истории требуют токен владельца. Он задан переменной OWNER_TOKEN в Render (Dashboard → сервис → Environment) и не хранится в коде.' }),
      el('p', {}, inp, ' ', save, ' ', out, ' ', st),
    );
  }

  // ---------- diagnostics ----------
  async function renderDiag(): Promise<void> {
    const t0 = performance.now();
    try {
      const d = await api<Record<string, any>>('/api/diag'); // eslint-disable-line @typescript-eslint/no-explicit-any
      const rtt = Math.round(performance.now() - t0);
      const s = store.status;
      const now = Date.now();
      const age = (t: number) => (t ? `${((now - t) / 1000).toFixed(1)} с назад` : '—');
      const streams = s?.streams ? Object.values(s.streams).map((x) => `<tr><td class="l">${esc(x.route === 'depth' ? 'стакан (/public)' : x.route === 'flow' ? 'сделки, mark, ликвидации (/market)' : 'все потоки')}</td><td class="l">${x.connected ? 'подключён' : '<span class="warn">нет</span>'}</td><td>${x.messages}</td><td>${age(x.lastMsgAt)}</td><td>${x.reconnects}</td><td class="l">${esc(x.url)}</td></tr>`).join('') : '';
      const v = s?.verify as Record<string, Record<string, number | string>> | undefined;
      const ps = s?.persist;
      const st = d.storage;
      const rest = { ...(d.exchangeRest ?? {}), ...(s?.rest ?? {}) } as Record<string, { ok: number; fail: number; lastStatus: number; lastError: string; bannedUntil: number }>;
      diagBox.innerHTML = `
        <div class="cards">
          <div class="card"><div class="k">Браузер ↔ Render</div><div class="v">${store.net.state === 'open' ? 'WebSocket открыт' : esc(store.net.state)}</div><div class="k">RTT ${isFinite(store.net.rtt) ? store.net.rtt : '—'} мс · HTTP ${rtt} мс</div></div>
          <div class="card"><div class="k">Синхронизация стакана (${esc(store.key)})</div><div class="v">${esc(s?.state ?? '—')}</div><div class="k">${s?.gate ? 'сигналы разрешены' : 'сигналы заблокированы: ' + esc(s?.gateReason ?? '')}</div></div>
          <div class="card"><div class="k">Последняя сделка / depth</div><div class="v">${store.lastTrade ? fmtDateTime(store.lastTrade.t) : '—'}</div><div class="k">depth обновлён ${age(s?.lastUpdate ?? 0)}</div></div>
          <div class="card"><div class="k">Задержка биржа→сервер</div><div class="v">${s && isFinite(s.latencyMs) ? s.latencyMs + ' мс' : '—'}</div><div class="k">оценка по часам сервера; разрывы ${s?.gaps ?? 0} · ресинхр. ${s?.resyncs ?? 0}</div></div>
          <div class="card"><div class="k">Сервер</div><div class="v">RAM ${d.memoryMB.rss} МБ</div><div class="k">event loop p99 ${d.eventLoopLagMs.p99} мс · uptime ${d.uptimeSec} с · ${esc(d.node)}</div></div>
          <div class="card"><div class="k">Supabase</div><div class="v">${d.supabase.connected ? 'подключено' : '<span class="warn">нет</span>'}</div><div class="k">${esc(d.supabase.error || d.supabase.host || '')}</div></div>
          <div class="card"><div class="k">Запись истории (${esc(store.key)})</div><div class="v">${!ps ? '—' : !ps.enabled ? '<span class="warn">не записывается</span>' : ps.lastError && (ps.lastErrorAt ?? 0) > ps.lastOkAt ? '<span class="warn">ошибка: ' + esc(ps.lastError) + '</span>' : 'ок'}</div><div class="k">${ps ? `очередь ${ps.queued}, несохранённый хвост ≈ ${Math.round(ps.oldestUnsavedMs / 1000)} с (включая открытый L2-блок архива, до 5 мин), отброшено ${ps.dropped}; последняя запись ${age(ps.lastOkAt)}` : ''}</div></div>
          <div class="card"><div class="k">Последний архив L2</div><div class="v">${ps?.archive.lastAt ? age(ps.archive.lastAt) : '—'}</div><div class="k">${ps ? `${ps.archive.blocks} блоков, ${mb(ps.archive.bytes)} за сессию; в очереди ${ps.archive.pending}` : ''}</div></div>
        </div>
        <h3>Потоки Render ↔ биржа</h3>
        <table><thead><tr><th class="l">Поток</th><th class="l">Состояние</th><th>Сообщений</th><th>Последнее</th><th>Переподкл.</th><th class="l">Адрес</th></tr></thead><tbody>${streams}</tbody></table>
        <h3>Живая сверка с биржей</h3>
        <p>${v ? `bookTicker vs локальный стакан при одинаковом updateId: <b>${v.bbo.matches}/${v.bbo.checks}</b> совпадений${v.bbo.lastMismatch ? ' · последнее расхождение: ' + esc(String(v.bbo.lastMismatch)) : ''}.<br>
        aggTrade: разрывов id ${v.trades.idGaps} (${v.trades.missingIds} id, догружено ${v.trades.backfilled}); выборка REST: ${v.trades.restMatches}/${v.trades.restSamples} совпадений${v.trades.restMismatch ? ' · ' + esc(String(v.trades.restMismatch)) : ''}.<br>
        Закрытые 1m-свечи из потока vs REST: OHLC ${v.candles.ohlcMatches}/${v.candles.compared}, объём ${v.candles.volumeWithin}/${v.candles.compared}${v.candles.lastDiff ? ' · ' + esc(String(v.candles.lastDiff)) : ''}.` : '—'}</p>
        <h3>REST биржи</h3>
        <p>${Object.entries(rest).map(([h, r]) => `${esc(h)}: ок ${r.ok}, ошибок ${r.fail}${r.bannedUntil > now ? ` · <span class="warn">IP сервера заблокирован биржей до ${fmtDateTime(r.bannedUntil)}</span>` : ''}${r.lastError ? ' · последняя ошибка: ' + esc(r.lastError.slice(0, 120)) : ''}`).join('<br>') || '—'}</p>
        <h3>Хранилище и квоты (Supabase Free)</h3>
        <p>${st ? `База: ${mb(st.dbBytes)} из бюджета ${mb(st.policy.dbBudgetBytes)} · Storage: ${st.storage ? mb(st.storage.bytes) + ' (' + st.storage.objects + ' объектов)' : '—'} из ${mb(st.policy.storageBudgetBytes)}${st.archivePaused ? ' · <span class="warn">подробный архив приостановлен (бюджет)</span>' : ''}${st.heat10Paused ? ' · <span class="warn">тайлы 10 с приостановлены (бюджет)</span>' : ''}.<br>
        Политика: тайлы heatmap 10 с — ${(st.policy.heat10Ms / 3600_000).toFixed(0)} ч, 60 с — ${(st.policy.heat60Ms / 86_400_000).toFixed(0)} д; события — ${(st.policy.eventsMs / 86_400_000).toFixed(0)} д; 1m-свечи — ${(st.policy.candlesMs / 86_400_000).toFixed(0)} д; исходные L2-блоки (точный replay) — ${(st.policy.archiveMs / 3600_000).toFixed(0)} ч. После удаления исходных блоков точный replay недоступен, остаются агрегаты.<br>
        ${st.lastArchiveCheck ? `Сверка архива: ${st.lastArchiveCheck.ok ? 'совпадает' : '<span class="warn">расхождение</span>'} (${esc(st.lastArchiveCheck.detail)}, ${fmtDateTime(st.lastArchiveCheck.at)})` : 'Сверка архива ещё не выполнялась.'}` : 'Supabase не подключён: постоянной истории нет.'}</p>
        <p class="muted">Render Free засыпает без входящего трафика и перезапускается; во время сна запись не ведётся — такие периоды отмечаются как пропуски. Локальный диск Render временный; постоянная история — только то, что подтверждено Supabase.</p>`;
    } catch (e) {
      diagBox.textContent = 'Диагностика недоступна: ' + (e as Error).message;
    }
  }

  // ---------- record list ----------
  async function renderRec(): Promise<void> {
    try {
      const r = await api<{ list: { source: string; symbol: string; enabled: boolean }[]; recording: string[]; maxSessions: number }>('/api/record-list');
      const inp = el('input', { placeholder: 'SYMBOL', style: 'width:120px;text-transform:uppercase', value: store.symbol });
      const add = el('button', { text: 'Записывать' });
      add.onclick = async () => {
        try {
          await api('/api/record-list', {}, { method: 'POST', body: JSON.stringify({ source: store.source, symbol: inp.value.trim().toUpperCase(), enabled: true }) });
          void renderRec();
        } catch (e) {
          alert((e as Error).message);
        }
      };
      recBox.replaceChildren(
        el('p', { class: 'muted', text: `Сервер постоянно записывает до ${r.maxSessions} инструментов (ограничение ресурсов Render Free) независимо от открытой вкладки. Сейчас: ${r.recording.join(', ') || '—'}.` }),
        ...r.list.map((x) => {
          const b = el('button', { text: x.enabled ? 'Остановить запись' : 'Включить запись' });
          b.onclick = async () => {
            try {
              await api('/api/record-list', {}, { method: 'POST', body: JSON.stringify({ source: x.source, symbol: x.symbol, enabled: !x.enabled }) });
              void renderRec();
            } catch (e) {
              alert((e as Error).message);
            }
          };
          return el('p', {}, `${x.source}:${x.symbol} — ${x.enabled ? 'записывается' : 'выключен'} `, b);
        }),
        el('p', {}, inp, ' ', add),
      );
    } catch (e) {
      recBox.textContent = 'Недоступно: ' + (e as Error).message;
    }
  }

  // ---------- sources ----------
  async function renderSources(): Promise<void> {
    try {
      const s = await api<SourcesResp>('/api/sources');
      srcBox.innerHTML =
        s.available
          .map(
            (a) =>
              `<div class="card" style="margin-bottom:8px"><b>${esc(a.name)}</b> <span class="muted">(${a.id}) · реализован и подключён</span><br><span class="muted">Потоки:</span> ${Object.entries(a.caps)
                .filter(([, v]) => v === true)
                .map(([k]) => k)
                .join(', ')}<br><span class="muted">Нативные интервалы свечей:</span> ${(a.caps.nativeIntervals as string[]).join(', ')}<ul>${a.limitations.map((l) => `<li>${esc(l)}</li>`).join('')}</ul></div>`,
          )
          .join('') +
        `<div class="card"><b>Ожидают доступа / не подключены</b><ul>${s.unavailable.map((u) => `<li><b>${esc(u.name)}</b>${u.status ? ` — <i>${esc(u.status)}</i>` : ''}: ${esc(u.reason)}${u.needs?.length ? '<br><span class="muted">Нужно: ' + u.needs.map(esc).join('; ') + '</span>' : ''}</li>`).join('')}</ul><p class="muted">Данные Binance никогда не смешиваются с данными CME/CFD. Контракты Binance на сырьё (например XAUUSDT) — собственный стакан Binance, а не CME/COMEX/OTC.</p></div>`;
    } catch (e) {
      srcBox.textContent = 'Не удалось загрузить источники: ' + (e as Error).message;
    }
  }

  async function renderConfig(): Promise<void> {
    try {
      const r = await api<{ config: Record<string, unknown> }>('/api/config', { source: store.source, symbol: store.symbol });
      const grid = el('div', { class: 'form-grid' });
      const inputs: [string, string | null, HTMLInputElement][] = [];
      const add = (sec: string | null, k: string, v: unknown) => {
        const inp = el('input', typeof v === 'boolean' ? { type: 'checkbox' } : { type: 'number', step: 'any', value: String(v) });
        if (typeof v === 'boolean') inp.checked = v;
        inputs.push([k, sec, inp]);
        grid.append(el('label', {}, `${sec ? sec + '.' : ''}${k}`, inp));
      };
      for (const [k, v] of Object.entries(r.config)) {
        if (v && typeof v === 'object') for (const [k2, v2] of Object.entries(v)) add(k, k2, v2);
        else add(null, k, v);
      }
      const saveBtn = el('button', { text: 'Сохранить (владелец)' });
      const msg = el('span', { class: 'muted' });
      saveBtn.onclick = async () => {
        const patch: Record<string, unknown> = {};
        for (const [k, sec, inp] of inputs) {
          const v = inp.type === 'checkbox' ? inp.checked : Number(inp.value);
          if (typeof v === 'number' && !isFinite(v)) continue;
          if (sec) ((patch[sec] ??= {}) as Record<string, unknown>)[k] = v;
          else patch[k] = v;
        }
        try {
          await api('/api/config', { source: store.source, symbol: store.symbol }, { method: 'PUT', body: JSON.stringify(patch) });
          msg.textContent = 'Сохранено в Supabase и применено к живому детектору.';
        } catch (e) {
          msg.textContent = 'Ошибка: ' + (e as Error).message;
        }
      };
      cfgBox.replaceChildren(el('p', { class: 'muted', text: 'Пороги адаптивные (перцентили, доля глубины, кратные ATR). Score 0–100 — эвристика, а не калиброванная вероятность.' }), grid, el('p', {}, saveBtn, ' ', msg));
    } catch (e) {
      cfgBox.textContent = 'Не удалось загрузить настройки: ' + (e as Error).message;
    }
  }

  let timer = 0;
  store.on('reset', () => void renderConfig());
  return {
    id: 'sources',
    title: 'Источники и настройки',
    root,
    show() {
      renderOwner();
      void renderDiag();
      void renderRec();
      void renderSources();
      void renderConfig();
      timer = window.setInterval(() => void renderDiag(), 3000);
    },
    hide() {
      clearInterval(timer);
    },
  };
}
