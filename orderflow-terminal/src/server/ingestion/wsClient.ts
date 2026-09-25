// Reconnecting WebSocket client with heartbeat (silence) detection and exponential backoff.
import WebSocket from 'ws';

export type WsState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface WsClientOptions {
  url: string | (() => string);
  /** terminate and reconnect if no message for this long */
  silenceMs?: number;
  /** send a ping frame every pingMs */
  pingMs?: number;
  backoffBaseMs?: number;
  backoffMaxMs?: number;
  /** reconnect proactively before the exchange's 24h connection limit */
  maxLifetimeMs?: number;
  /** injectable for tests */
  factory?: (url: string) => WebSocket;
  onOpen?: () => void;
  onMessage?: (data: string) => void;
  onClose?: (code: number, reason: string, willReconnect: boolean) => void;
  onState?: (s: WsState) => void;
  onError?: (err: Error) => void;
}

export function backoffDelay(attempt: number, base: number, max: number, salt = 0): number {
  const exp = Math.min(max, base * 2 ** Math.max(0, attempt - 1));
  // deterministic jitter in [0.75, 1.0] of exp — avoids thundering herd without random numbers
  const j = 0.75 + 0.25 * (((attempt * 2654435761 + salt) >>> 0) % 1000) / 1000;
  return Math.round(exp * j);
}

export class ReconnectingWs {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private timer: NodeJS.Timeout | null = null;
  private watchdog: NodeJS.Timeout | null = null;
  private pinger: NodeJS.Timeout | null = null;
  private lifetime: NodeJS.Timeout | null = null;
  private stopped = false;
  lastMessageAt = 0;
  state: WsState = 'idle';
  reconnects = 0;
  messages = 0;

  constructor(private o: WsClientOptions) {}

  private setState(s: WsState): void {
    this.state = s;
    this.o.onState?.(s);
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  private connect(): void {
    this.clearTimers();
    const url = typeof this.o.url === 'function' ? this.o.url() : this.o.url;
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');
    let ws: WebSocket;
    try {
      ws = this.o.factory ? this.o.factory(url) : new WebSocket(url, { perMessageDeflate: false, handshakeTimeout: 10_000 });
    } catch (e) {
      this.o.onError?.(e as Error);
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.on('open', () => {
      this.attempt = 0;
      this.lastMessageAt = Date.now();
      this.setState('open');
      this.armWatchdog();
      if (this.o.pingMs) this.pinger = setInterval(() => ws.readyState === WebSocket.OPEN && ws.ping(), this.o.pingMs);
      if (this.o.maxLifetimeMs) this.lifetime = setTimeout(() => ws.terminate(), this.o.maxLifetimeMs);
      this.o.onOpen?.();
    });
    ws.on('message', (data: WebSocket.RawData) => {
      this.lastMessageAt = Date.now();
      this.messages++;
      this.o.onMessage?.(data.toString());
    });
    ws.on('pong', () => {
      this.lastMessageAt = Date.now();
    });
    ws.on('error', (err: Error) => this.o.onError?.(err));
    ws.on('close', (code: number, reason: Buffer) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.clearTimers();
      const will = !this.stopped;
      this.o.onClose?.(code, reason.toString(), will);
      if (will) this.scheduleReconnect();
      else this.setState('closed');
    });
  }

  private armWatchdog(): void {
    const silence = this.o.silenceMs ?? 15_000;
    this.watchdog = setInterval(() => {
      if (Date.now() - this.lastMessageAt > silence) {
        this.o.onError?.(new Error(`no data for ${silence} ms — reconnecting`));
        this.ws?.terminate();
      }
    }, Math.min(1000, silence / 2));
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.attempt++;
    this.reconnects++;
    this.setState('reconnecting');
    const d = backoffDelay(this.attempt, this.o.backoffBaseMs ?? 1000, this.o.backoffMaxMs ?? 30_000, this.reconnects);
    this.timer = setTimeout(() => this.connect(), d);
  }

  /** Force a reconnect now (e.g. after persistent staleness). */
  restart(): void {
    if (this.ws) this.ws.terminate();
    else if (!this.timer) this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.removeAllListeners('message');
      try {
        ws.close();
      } catch {
        ws.terminate();
      }
    }
    this.setState('closed');
  }

  private clearTimers(): void {
    for (const t of [this.timer, this.watchdog, this.lifetime]) if (t) clearTimeout(t);
    if (this.pinger) clearInterval(this.pinger);
    if (this.watchdog) clearInterval(this.watchdog);
    this.timer = this.watchdog = this.pinger = this.lifetime = null;
  }
}
