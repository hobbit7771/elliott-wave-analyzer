// TEST-ONLY synthetic records in Databento's documented JSON layout (no real data, no network).
import http from 'node:http';
import { databentoRange, dbnMbp10, dbnTrade, type DbnJsonRecord } from '../src/server/adapters/databento.js';

const hd = { ts_event: '2026-09-25T14:30:00.123456789Z', rtype: 0, publisher_id: 1, instrument_id: 42 };

describe('Databento adapter (format-level, not verified on real data)', () => {
  it('maps trade aggressor side and drops non-aggressor prints', () => {
    expect(dbnTrade({ hd, action: 'T', side: 'B', price: '21500.25', size: 3, sequence: 7 })).toEqual({ t: Date.parse(hd.ts_event), price: 21500.25, qty: 3, side: 1, id: 7 });
    expect(dbnTrade({ hd, action: 'T', side: 'A', price: '21500.00', size: 1 })?.side).toBe(-1);
    expect(dbnTrade({ hd, action: 'T', side: 'N', price: '21500.00', size: 1 })).toBeNull();
    expect(dbnTrade({ hd, action: 'A', side: 'B', price: '21500.00', size: 1 })).toBeNull();
  });

  it('maps MBP-10 levels and skips empty ones', () => {
    const r: DbnJsonRecord = {
      hd, action: 'A', side: 'B', price: '100.25', size: 5,
      levels: [
        { bid_px: '100.25', ask_px: '100.50', bid_sz: 5, ask_sz: 7, bid_ct: 2, ask_ct: 3 },
        { bid_px: '100.00', ask_px: null, bid_sz: 9, ask_sz: 0, bid_ct: 1, ask_ct: 0 },
      ],
    };
    expect(dbnMbp10(r)).toMatchObject({ bids: [[100.25, 5], [100, 9]], asks: [[100.5, 7]] });
  });

  it('streams NDJSON across chunk boundaries and sends basic auth + form params', async () => {
    let auth = '';
    let form = '';
    const lines = [{ hd, action: 'T', side: 'B', price: '1.5', size: 1 }, { hd, action: 'T', side: 'A', price: '1.25', size: 2 }].map((x) => JSON.stringify(x)).join('\n') + '\n';
    const srv = http.createServer(async (req, res) => {
      auth = req.headers.authorization ?? '';
      for await (const c of req) form += c;
      res.write(lines.slice(0, 17));
      setTimeout(() => res.end(lines.slice(17)), 20);
    });
    await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
    const base = `http://127.0.0.1:${(srv.address() as { port: number }).port}`;
    const out = [];
    for await (const r of databentoRange({ key: 'db-test', dataset: 'GLBX.MDP3', symbol: 'NQ.c.0', schema: 'trades', start: '2026-09-25T14:00', end: '2026-09-25T14:01' }, base)) out.push(dbnTrade(r));
    srv.close();
    expect(out.map((t) => t?.side)).toEqual([1, -1]);
    expect(Buffer.from(auth.replace('Basic ', ''), 'base64').toString()).toBe('db-test:');
    expect(new URLSearchParams(form).get('stype_in')).toBe('continuous');
    expect(new URLSearchParams(form).get('encoding')).toBe('json');
  });
});
