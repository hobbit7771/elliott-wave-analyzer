import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { getJson, restHealth } from '../src/server/adapters/adapter.js';

describe('REST ban guard', () => {
  it('sends nothing to a host while its IP ban lasts (Binance extends bans for requests during one)', async () => {
    let hits = 0;
    const until = Date.now() + 60_000;
    const srv = http.createServer((_req, res) => {
      hits++;
      res.writeHead(418, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ code: -1003, msg: `Way too many requests; IP(1.2.3.4) banned until ${until}.` }));
    });
    await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
    const url = `http://127.0.0.1:${(srv.address() as AddressInfo).port}/fapi/v1/klines`;
    await expect(getJson(url)).rejects.toThrow(/418/);
    await expect(getJson(url)).rejects.toThrow(/paused until/);
    await expect(getJson(url)).rejects.toThrow(/paused until/);
    expect(hits).toBe(1);
    expect(restHealth[new URL(url).host].bannedUntil).toBe(until);
    srv.close();
  });

  it('pauses after a 429 for Retry-After seconds', async () => {
    let hits = 0;
    const srv = http.createServer((_req, res) => {
      hits++;
      res.writeHead(429, { 'retry-after': '30' });
      res.end('{}');
    });
    await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
    const url = `http://127.0.0.1:${(srv.address() as AddressInfo).port}/x`;
    const t = Date.now();
    await expect(getJson(url)).rejects.toThrow(/429/);
    await expect(getJson(url)).rejects.toThrow(/paused until/);
    expect(hits).toBe(1);
    const bu = restHealth[new URL(url).host].bannedUntil;
    expect(bu - t).toBeGreaterThanOrEqual(29_000);
    expect(bu - t).toBeLessThan(40_000);
    srv.close();
  });
});
