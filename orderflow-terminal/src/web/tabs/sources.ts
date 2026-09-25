// Data sources, their real capabilities & limitations, detector settings per instrument, performance.
import type { Tab } from '../main.js';
import { store } from '../store.js';
import { api, el, esc } from '../util.js';

interface SourcesResp {
  available: { id: string; name: string; caps: Record<string, unknown>; limitations: string[] }[];
  unavailable: { id: string; name: string; reason: string }[];
}

export function createSourcesTab(): Tab {
  const root = el('section', { id: 'tab-sources', role: 'tabpanel' });
  const body = el('div', { class: 'scroll pad' });
  root.append(body);
  const srcBox = el('div');
  const cfgBox = el('div');
  const perfBox = el('div');
  const purgeBtn = el('button', { text: 'Delete recorded history for this instrument' });
  body.append(el('h3', { text: 'Data sources' }), srcBox, el('h3', { text: 'Detector settings (current instrument)' }), cfgBox, el('h3', { text: 'Server performance' }), perfBox, el('p', {}, purgeBtn));

  async function loadSources(): Promise<void> {
    try {
      const s = await api<SourcesResp>('/api/sources');
      srcBox.innerHTML =
        s.available
          .map(
            (a) =>
              `<div class="card" style="margin-bottom:8px"><b>${esc(a.name)}</b> <span class="muted">(${a.id})</span><br><span class="muted">Streams:</span> ${Object.entries(a.caps)
                .filter(([, v]) => v === true)
                .map(([k]) => k)
                .join(', ')}<br><span class="muted">Native kline intervals:</span> ${(a.caps.nativeIntervals as string[]).join(', ')}<ul>${a.limitations.map((l) => `<li>${esc(l)}</li>`).join('')}</ul></div>`,
          )
          .join('') +
        `<div class="card"><b>Not available (by design)</b><ul>${s.unavailable.map((u) => `<li><b>${esc(u.name)}</b> — ${esc(u.reason)}</li>`).join('')}</ul><p class="muted">Binance data is never mixed with CME / CFD data. Binance commodity contracts (e.g. XAUUSDT) show Binance's own order book only.</p></div>`;
    } catch (e) {
      srcBox.textContent = 'Could not load sources: ' + (e as Error).message;
    }
  }

  async function loadConfig(): Promise<void> {
    try {
      const r = await api<{ config: Record<string, unknown> }>('/api/config', { source: store.source, symbol: store.symbol });
      cfgBox.replaceChildren(configForm(r.config));
    } catch (e) {
      cfgBox.textContent = 'Could not load settings: ' + (e as Error).message;
    }
  }

  function configForm(cfg: Record<string, unknown>): HTMLElement {
    const grid = el('div', { class: 'form-grid' });
    const inputs: [string, string | null, HTMLInputElement][] = [];
    const add = (sec: string | null, k: string, v: unknown) => {
      const inp = el('input', typeof v === 'boolean' ? { type: 'checkbox' } : { type: 'number', step: 'any', value: String(v) });
      if (typeof v === 'boolean') inp.checked = v;
      inputs.push([k, sec, inp]);
      grid.append(el('label', {}, `${sec ? sec + '.' : ''}${k}`, inp));
    };
    for (const [k, v] of Object.entries(cfg)) {
      if (v && typeof v === 'object') for (const [k2, v2] of Object.entries(v)) add(k, k2, v2);
      else add(null, k, v);
    }
    const saveBtn = el('button', { text: 'Save settings' });
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
        await api('/api/config', { source: store.source, symbol: store.symbol }, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(patch) });
        msg.textContent = 'Saved — applied to the live detector.';
      } catch (e) {
        msg.textContent = 'Failed: ' + (e as Error).message;
      }
    };
    return el('div', {}, el('p', { class: 'muted', text: 'Thresholds are adaptive (percentiles, % of depth, ATR multiples). Per-instrument overrides are stored on the server and applied immediately.' }), grid, el('p', {}, saveBtn, ' ', msg));
  }

  async function loadPerf(): Promise<void> {
    try {
      const p = await api<Record<string, unknown>>('/api/perf');
      perfBox.innerHTML = `<pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(p, null, 1))}</pre>`;
    } catch (e) {
      perfBox.textContent = 'perf unavailable: ' + (e as Error).message;
    }
  }

  purgeBtn.onclick = async () => {
    if (!confirm(`Delete all recorded trades, heatmap and events for ${store.key} on the server?`)) return;
    await api('/api/history', { source: store.source, symbol: store.symbol }, { method: 'DELETE' });
    store.heat = [];
    store.emit('heat');
    alert('History deleted.');
  };
  let timer = 0;
  store.on('reset', () => void loadConfig());
  return {
    id: 'sources',
    title: 'Sources & Settings',
    root,
    show() {
      void loadSources();
      void loadConfig();
      void loadPerf();
      timer = window.setInterval(() => void loadPerf(), 5000);
    },
    hide() {
      clearInterval(timer);
    },
  };
}
