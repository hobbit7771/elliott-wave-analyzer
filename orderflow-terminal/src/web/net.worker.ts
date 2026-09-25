/// <reference lib="webworker" />
// Web Worker: owns the server WebSocket (reconnect + ping), parses JSON off the UI thread and
// hands batched, coalesced updates to the UI at most every 100 ms (backpressure: while the UI has
// not acknowledged a batch, new data keeps coalescing; the latest book always replaces older ones).

type Msg = { ch: string; k?: string; d: unknown };

let ws: WebSocket | null = null;
let url = '';
let attempt = 0;
let subs: { source: string; symbol: string }[] = [];
let pending: Msg[] = [];
const latest = new Map<string, Msg>(); // coalesced channels
let trades: unknown[] = [];
let tradeKey = '';
let received = 0;
let dropped = 0;
let uiBusy = false;
let lastPost = 0;
let pingTimer: ReturnType<typeof setInterval> | null = null;
const COALESCE = new Set(['book', 'status', 'large', 'clusters', 'ice', 'deriv']);
const MAX_PENDING = 20_000;

function connect(): void {
  try {
    ws = new WebSocket(url);
  } catch {
    retry();
    return;
  }
  post({ ch: 'net', d: { state: attempt ? 'reconnecting' : 'connecting' } });
  ws.onopen = () => {
    attempt = 0;
    post({ ch: 'net', d: { state: 'open' } });
    for (const s of subs) ws!.send(JSON.stringify({ op: 'sub', ...s }));
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => ws?.readyState === 1 && ws.send(JSON.stringify({ op: 'ping', t: Date.now() })), 5000);
    ws!.send(JSON.stringify({ op: 'ping', t: Date.now() }));
  };
  ws.onmessage = (e) => {
    received++;
    let m: Msg;
    try {
      m = JSON.parse(e.data as string);
    } catch {
      return;
    }
    if (m.ch === 'pong') {
      const d = m.d as { t: number; st: number };
      const now = Date.now();
      post({ ch: 'rtt', d: { rtt: now - d.t, offset: d.st - (d.t + now) / 2 } });
      return;
    }
    if (m.ch === 'trades') {
      if (tradeKey !== m.k) {
        trades = [];
        tradeKey = m.k ?? '';
      }
      for (const t of m.d as unknown[]) trades.push(t);
      if (trades.length > 200_000) {
        dropped += trades.length - 200_000;
        trades.splice(0, trades.length - 200_000);
      }
    } else if (COALESCE.has(m.ch)) latest.set(m.ch + '|' + m.k, m);
    else {
      pending.push(m);
      if (pending.length > MAX_PENDING) {
        dropped += pending.length - MAX_PENDING;
        pending.splice(0, pending.length - MAX_PENDING);
      }
    }
    flushSoon();
  };
  ws.onclose = () => {
    if (pingTimer) clearInterval(pingTimer);
    ws = null;
    retry();
  };
  ws.onerror = () => ws?.close();
}

function retry(): void {
  attempt++;
  const d = Math.min(15_000, 500 * 2 ** Math.min(attempt, 5));
  post({ ch: 'net', d: { state: 'reconnecting', inMs: d } });
  setTimeout(connect, d);
}

let flushTimer: ReturnType<typeof setTimeout> | null = null;
function flushSoon(): void {
  if (flushTimer) return;
  const wait = Math.max(0, 100 - (Date.now() - lastPost));
  flushTimer = setTimeout(flush, wait);
}

function flush(): void {
  flushTimer = null;
  if (uiBusy) {
    flushSoon();
    return;
  }
  const batch: Msg[] = [];
  // order: events/heat first (keep sequence), then coalesced snapshots, then trades
  batch.push(...pending);
  pending = [];
  for (const m of latest.values()) batch.push(m);
  latest.clear();
  if (trades.length) {
    batch.push({ ch: 'trades', k: tradeKey, d: trades });
    trades = [];
  }
  if (!batch.length) return;
  uiBusy = true;
  lastPost = Date.now();
  (self as unknown as Worker).postMessage({ batch, stats: { received, dropped } });
}

function post(m: Msg): void {
  (self as unknown as Worker).postMessage({ batch: [m], stats: { received, dropped } });
}

self.onmessage = (e: MessageEvent) => {
  const m = e.data as { op: string; url?: string; source?: string; symbol?: string };
  if (m.op === 'init' && m.url) {
    url = m.url;
    connect();
  } else if (m.op === 'ack') {
    uiBusy = false;
    if (pending.length || latest.size || trades.length) flushSoon();
  } else if (m.op === 'sub' && m.source && m.symbol) {
    for (const s of subs) ws?.readyState === 1 && ws.send(JSON.stringify({ op: 'unsub', ...s }));
    subs = [{ source: m.source, symbol: m.symbol }];
    pending = [];
    latest.clear();
    trades = [];
    if (ws?.readyState === 1) ws.send(JSON.stringify({ op: 'sub', source: m.source, symbol: m.symbol }));
  }
};
