import { WebSocketServer } from 'ws';
import type { AddressInfo } from 'node:net';
import { ReconnectingWs, backoffDelay } from '../src/server/ingestion/wsClient.js';

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function until(fn: () => boolean, ms = 5000): Promise<void> {
  const t = Date.now();
  while (!fn()) {
    if (Date.now() - t > ms) throw new Error('timeout');
    await wait(20);
  }
}

describe('Reconnecting WebSocket client', () => {
  it('backoff grows exponentially, is capped and jittered deterministically', () => {
    const d = [1, 2, 3, 4, 5, 6, 10].map((a) => backoffDelay(a, 1000, 30_000));
    expect(d[0]).toBeGreaterThanOrEqual(750);
    expect(d[0]).toBeLessThanOrEqual(1000);
    expect(d[3]).toBeGreaterThanOrEqual(6000);
    expect(d[6]).toBeLessThanOrEqual(30_000);
    expect(backoffDelay(3, 1000, 30_000)).toBe(backoffDelay(3, 1000, 30_000));
  });

  it('reconnects after the server drops the connection', async () => {
    const wss = new WebSocketServer({ port: 0 });
    const port = (wss.address() as AddressInfo).port;
    let conns = 0;
    wss.on('connection', (ws) => {
      conns++;
      ws.send('hello');
      if (conns === 1) setTimeout(() => ws.terminate(), 50);
    });
    const states: string[] = [];
    const msgs: string[] = [];
    const c = new ReconnectingWs({ url: `ws://127.0.0.1:${port}`, backoffBaseMs: 50, onState: (s) => states.push(s), onMessage: (m) => msgs.push(m) });
    c.start();
    await until(() => conns >= 2 && c.state === 'open');
    expect(states).toContain('reconnecting');
    expect(c.reconnects).toBe(1);
    expect(msgs).toEqual(['hello', 'hello']);
    c.stop();
    await new Promise((r) => wss.close(r));
  });

  it('terminates and reconnects a silent (half-open) connection', async () => {
    const wss = new WebSocketServer({ port: 0 });
    const port = (wss.address() as AddressInfo).port;
    let conns = 0;
    wss.on('connection', () => conns++); // never sends anything
    const errors: string[] = [];
    const c = new ReconnectingWs({ url: `ws://127.0.0.1:${port}`, silenceMs: 300, backoffBaseMs: 50, onError: (e) => errors.push(e.message) });
    c.start();
    await until(() => conns >= 2, 5000);
    expect(errors.some((e) => /no data/.test(e))).toBe(true);
    c.stop();
    await new Promise((r) => wss.close(r));
  });

  it('keeps retrying while the endpoint is down', async () => {
    const c = new ReconnectingWs({ url: 'ws://127.0.0.1:9', backoffBaseMs: 20, backoffMaxMs: 40 });
    c.start();
    await until(() => c.reconnects >= 3);
    expect(c.state).toBe('reconnecting');
    c.stop();
    expect(c.state).toBe('closed');
  });
});
