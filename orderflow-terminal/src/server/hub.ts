// Session manager + client fan-out with backpressure.
import { Worker } from 'node:worker_threads';
import type WebSocket from 'ws';
import type { InstrumentMeta, SourceId } from '../core/types.js';
import { type DetectorConfig, DEFAULT_DETECTOR_CONFIG, mergeConfig, type DeepPartial } from '../core/detectors/config.js';
import { getAdapter } from './adapters/registry.js';
import { type HistoryReader, symKey } from './recorder.js';

export interface HubOptions {
  workerPath: string;
  dbPath: string;
  maxSessions: number;
  idleStopMs: number;
  pinned: string[];
  backfillMinutes: number;
  reader: HistoryReader;
  log: (m: string) => void;
}

interface Client {
  ws: WebSocket;
  subs: Set<string>;
  dropped: number;
  lagging: boolean;
  lagSince: number;
}

interface Live {
  key: string;
  meta: InstrumentMeta;
  worker: Worker;
  subs: Set<Client>;
  last: Map<string, string>;
  recentTrades: string[];
  idleSince: number;
  startedAt: number;
  msgs: number;
  lastUsed: number;
}

const CACHED = new Set(['book', 'status', 'large', 'clusters', 'ice', 'deriv']);
const DROPPABLE = new Set(['book', 'heat', 'ice', 'large', 'clusters', 'deriv', 'status']);
const SOFT_LIMIT = 1 << 20; // 1 MiB buffered => drop droppable channels
const HARD_LIMIT = 8 << 20; // 8 MiB => client too slow, disconnect

export class Hub {
  sessions = new Map<string, Live>();
  clients = new Set<Client>();
  sent = 0;
  droppedTotal = 0;
  private timer: NodeJS.Timeout;

  constructor(private o: HubOptions) {
    this.timer = setInterval(() => this.reap(), 30_000);
  }

  config(key: string): DetectorConfig {
    return mergeConfig(DEFAULT_DETECTOR_CONFIG, this.o.reader.getSetting<DeepPartial<DetectorConfig>>('cfg:' + key));
  }

  setConfig(key: string, patch: DeepPartial<DetectorConfig>): DetectorConfig {
    const prev = this.o.reader.getSetting<DeepPartial<DetectorConfig>>('cfg:' + key) ?? {};
    const merged = mergeConfig(mergeConfig(DEFAULT_DETECTOR_CONFIG, prev), patch);
    this.o.reader.setSetting('cfg:' + key, merged);
    this.sessions.get(key)?.worker.postMessage({ op: 'config', cfg: merged });
    return merged;
  }

  async ensure(source: SourceId, symbol: string): Promise<Live> {
    const key = symKey(source, symbol);
    const cur = this.sessions.get(key);
    if (cur) {
      cur.lastUsed = Date.now();
      return cur;
    }
    const list = await getAdapter(source).listInstruments();
    const meta = list.find((m) => m.symbol === symbol);
    if (!meta) throw new Error(`unknown instrument ${symbol} on ${source}`);
    if (this.sessions.size >= this.o.maxSessions) {
      // stop the least recently used unpinned session without subscribers
      const idle = [...this.sessions.values()].filter((s) => !s.subs.size && !this.o.pinned.includes(s.key)).sort((a, b) => a.lastUsed - b.lastUsed)[0];
      if (!idle) throw new Error(`session limit reached (${this.o.maxSessions}); close another instrument first`);
      this.stop(idle.key);
    }
    const worker = new Worker(this.o.workerPath, {
      workerData: { meta, cfg: this.config(key), dbPath: this.o.dbPath, backfillMinutes: this.o.backfillMinutes },
      execArgv: ['--disable-warning=ExperimentalWarning'],
    });
    const live: Live = { key, meta, worker, subs: new Set(), last: new Map(), recentTrades: [], idleSince: Date.now(), startedAt: Date.now(), msgs: 0, lastUsed: Date.now() };
    worker.on('message', (m: { op: string; ch?: string; s?: string; m?: string }) => {
      if (m.op === 'pub' && m.ch && m.s) this.onPub(live, m.ch, m.s);
      else if (m.op === 'log' && m.m) this.o.log(m.m);
    });
    worker.on('error', (e) => this.o.log(`[${key}] worker error: ${e.stack ?? e.message}`));
    worker.on('exit', (code) => {
      this.o.log(`[${key}] worker exited ${code}`);
      if (this.sessions.get(key) === live) {
        this.sessions.delete(key);
        const status = JSON.stringify({ ch: 'status', k: key, d: { source, symbol, state: 'disconnected', message: `ingestion worker exited (${code})`, synced: false } });
        for (const c of live.subs) this.send(c, 'status', status);
        // automatic restart for subscribed / pinned instruments
        if (live.subs.size || this.o.pinned.includes(key)) {
          setTimeout(() => {
            void this.ensure(source, symbol).then((nl) => {
              for (const c of live.subs) nl.subs.add(c);
            }, (err) => this.o.log(`[${key}] restart failed: ${err.message}`));
          }, 3000);
        }
      }
    });
    this.sessions.set(key, live);
    this.o.log(`[${key}] session started`);
    return live;
  }

  private onPub(live: Live, ch: string, s: string): void {
    live.msgs++;
    if (CACHED.has(ch)) live.last.set(ch, s);
    if (ch === 'trades') {
      live.recentTrades.push(s);
      if (live.recentTrades.length > 50) live.recentTrades.shift();
    }
    for (const c of live.subs) this.send(c, ch, s);
  }

  private send(c: Client, ch: string, s: string): void {
    const ws = c.ws;
    if (ws.readyState !== 1) return;
    const buf = ws.bufferedAmount;
    if (buf > HARD_LIMIT) {
      this.o.log('client too slow, disconnecting');
      ws.terminate();
      return;
    }
    if (buf > SOFT_LIMIT) {
      if (!c.lagging) {
        c.lagging = true;
        c.lagSince = Date.now();
      }
      if (DROPPABLE.has(ch) || ch === 'trades') {
        c.dropped++;
        this.droppedTotal++;
        return;
      }
    } else if (c.lagging) {
      c.lagging = false;
      // trades were dropped: tell the client which window to reload from REST
      ws.send(JSON.stringify({ ch: 'gap', d: { from: c.lagSince - 1000, to: Date.now(), dropped: c.dropped } }));
    }
    ws.send(s);
    this.sent++;
  }

  addClient(ws: WebSocket): Client {
    const c: Client = { ws, subs: new Set(), dropped: 0, lagging: false, lagSince: 0 };
    this.clients.add(c);
    ws.on('close', () => {
      for (const k of c.subs) this.sessions.get(k)?.subs.delete(c);
      this.clients.delete(c);
    });
    ws.on('message', (raw) => void this.onClientMsg(c, raw.toString()));
    return c;
  }

  private async onClientMsg(c: Client, raw: string): Promise<void> {
    let m: { op?: string; source?: string; symbol?: string; t?: number };
    try {
      m = JSON.parse(raw);
    } catch {
      return;
    }
    if (m.op === 'ping') {
      c.ws.send(JSON.stringify({ ch: 'pong', d: { t: m.t, st: Date.now() } }));
      return;
    }
    if ((m.op === 'sub' || m.op === 'unsub') && typeof m.source === 'string' && typeof m.symbol === 'string') {
      if (!/^[A-Z0-9]{2,30}$/.test(m.symbol) || !['binance-futures', 'binance-spot'].includes(m.source)) return;
      const key = symKey(m.source, m.symbol);
      if (m.op === 'unsub') {
        this.sessions.get(key)?.subs.delete(c);
        c.subs.delete(key);
        return;
      }
      try {
        const live = await this.ensure(m.source as SourceId, m.symbol);
        live.subs.add(c);
        c.subs.add(key);
        live.idleSince = 0;
        c.ws.send(JSON.stringify({ ch: 'subscribed', k: key, d: live.meta }));
        for (const s of live.last.values()) c.ws.send(s);
        for (const s of live.recentTrades) c.ws.send(s);
      } catch (e) {
        c.ws.send(JSON.stringify({ ch: 'error', k: key, d: { message: (e as Error).message } }));
      }
    }
  }

  private reap(): void {
    const now = Date.now();
    for (const s of this.sessions.values()) {
      if (s.subs.size || this.o.pinned.includes(s.key)) {
        s.idleSince = 0;
        continue;
      }
      if (!s.idleSince) s.idleSince = now;
      else if (now - s.idleSince > this.o.idleStopMs) this.stop(s.key);
    }
  }

  stop(key: string): void {
    const s = this.sessions.get(key);
    if (!s) return;
    this.sessions.delete(key);
    s.worker.postMessage({ op: 'stop' });
    setTimeout(() => void s.worker.terminate(), 3000);
    this.o.log(`[${key}] session stopped`);
  }

  info(): unknown {
    return {
      clients: this.clients.size,
      sent: this.sent,
      dropped: this.droppedTotal,
      sessions: [...this.sessions.values()].map((s) => ({
        key: s.key,
        subscribers: s.subs.size,
        startedAt: s.startedAt,
        messagesFromWorker: s.msgs,
        status: s.last.has('status') ? JSON.parse(s.last.get('status')!).d : null,
      })),
    };
  }

  shutdown(): void {
    clearInterval(this.timer);
    for (const k of [...this.sessions.keys()]) this.stop(k);
  }
}
