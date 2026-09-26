// End-to-end: real server process + real Chromium, fed by the local test venue.
// Checks that every tab renders live data, markers appear, and layouts fit iPhone and desktop.
import { mkdirSync } from 'node:fs';
import { chromium, type Browser, type Page } from 'playwright-core';
import { startTestVenue, type TestVenue } from './testVenue.js';
import { OWNER, ARCHIVE_KEY, startServer, type ServerHandle } from './harness.js';
import { startPg, startArchiveServer } from '../fixtures/pg.js';

let BASE = '';
const OUT = 'test-results';
let venue: TestVenue;
let server: ServerHandle;
let pg: Awaited<ReturnType<typeof startPg>>;
let arch: Awaited<ReturnType<typeof startArchiveServer>>;
let browser: Browser;

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
const T = { source: 'binance-futures', symbol: 'TESTUSDT' };
const q = (extra: Record<string, string> = {}) => new URLSearchParams({ ...T, ...extra }).toString();

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  pg = await startPg();
  arch = await startArchiveServer(ARCHIVE_KEY);
  venue = await startTestVenue();
  server = await startServer({ venueUrl: venue.url, venueWs: venue.wsUrl, pgPort: pg.port, archiveUrl: arch.url });
  BASE = server.base;
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  // let the server record some heatmap columns / trades first
  await wait(8000);
});

afterAll(async () => {
  await browser?.close();
  await server?.stop();
  await venue?.close();
  await arch?.stop();
  await pg?.stop();
});

async function openApp(viewport: { width: number; height: number }, mobile: boolean): Promise<{ page: Page; errors: string[] }> {
  const ctx = await browser.newContext({ viewport, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: mobile ? 3 : 1 });
  await ctx.addInitScript((owner) => {
    localStorage.setItem('oft:instrument', JSON.stringify({ source: 'binance-futures', symbol: 'TESTUSDT' }));
    localStorage.setItem('oft:owner', owner);
  }, OWNER);
  const page = await ctx.newPage();
  const errors: string[] = [];
  page.on('pageerror', (e) => {
    console.log('PAGEERROR_STACK ' + JSON.stringify(e.stack ?? e.message));
    errors.push('pageerror: ' + e.message);
  });
  page.on('console', (m) => m.type() === 'error' && errors.push('console: ' + m.text()));
  page.on('response', (r) => r.status() >= 400 && errors.push(`http ${r.status()} ${r.url()}`));
  await page.goto(BASE + '/#chart');
  await page.waitForFunction(() => document.querySelector('.badge')?.textContent === 'LIVE', null, { timeout: 30_000 });
  return { page, errors };
}

async function noHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
}

async function canvasInk(page: Page, sel: string): Promise<number> {
  return page.evaluate((s) => {
    const c = document.querySelector(s) as HTMLCanvasElement | null;
    if (!c || !c.width) return 0;
    const d = c.getContext('2d')!.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 16) if (d[i] + d[i + 1] + d[i + 2] > 120) n++;
    return n;
  }, sel);
}

for (const [name, viewport, mobile] of [
  ['iphone', { width: 390, height: 844 }, true],
  ['desktop', { width: 1440, height: 900 }, false],
] as const) {
  describe(`UI on ${name}`, () => {
    it('streams live data into every tab without errors and fits the screen', async () => {
      const { page, errors } = await openApp(viewport, mobile);
      // status bar shows real counters
      await page.waitForFunction(() => Number(document.querySelectorAll('.statusbar b')[4]?.textContent) > 0, null, { timeout: 20_000 });
      expect(await noHorizontalOverflow(page)).toBe(true);

      // chart: candles + legend
      await page.waitForSelector('#tab-chart canvas', { timeout: 15_000 });
      await page.waitForFunction(() => /O \d/.test(document.querySelector('#tab-chart .legend')?.textContent ?? ''), null, { timeout: 15_000 });
      await page.screenshot({ path: `${OUT}/${name}-chart.png` });

      // coin picker: every instrument, searchable; tapping a row selects it
      await page.click('#symbolInput');
      await page.waitForSelector('.picker-row', { timeout: 5000 });
      await page.fill('.picker-head input', 'test');
      expect(await page.textContent('.picker-list')).toContain('TESTUSDT');
      await page.screenshot({ path: `${OUT}/${name}-picker.png` });
      await page.click('.picker-row');
      expect(await page.isHidden('.picker')).toBe(true);
      expect(await page.inputValue('#symbolInput')).toBe('TESTUSDT');

      for (const tab of ['heatmap', 'dom', 'footprint', 'profile', 'signals', 'levels', 'paper', 'alerts', 'sources']) {
        await page.click(`nav.tabs button[data-tab="${tab}"]`);
        await wait(tab === 'heatmap' ? 2500 : 1200);
        expect(await page.isVisible(`#tab-${tab}`)).toBe(true);
        expect(await noHorizontalOverflow(page), `overflow on ${tab}`).toBe(true);
        await page.screenshot({ path: `${OUT}/${name}-${tab}.png` });
      }

      // heatmap canvas actually painted liquidity
      await page.click('nav.tabs button[data-tab="heatmap"]');
      await wait(1500);
      expect(await canvasInk(page, '#tab-heatmap canvas')).toBeGreaterThan(500);
      // recorded history (not only columns received since page load) is shown
      const cols = Number((await page.textContent('#tab-heatmap .toolbar .muted'))?.match(/(\d+) колонок/)?.[1] ?? 0);
      expect(cols).toBeGreaterThanOrEqual(10);

      // DOM shows real bid / ask ladder
      await page.click('nav.tabs button[data-tab="dom"]');
      await wait(800);
      expect(await canvasInk(page, '#tab-dom canvas')).toBeGreaterThan(200);
      const lastTrade = await page.textContent('#tab-dom .card .v');
      expect(lastTrade).toMatch(/\d/);

      // footprint painted
      await page.click('nav.tabs button[data-tab="footprint"]');
      await wait(1200);
      expect(await canvasInk(page, '#tab-footprint canvas')).toBeGreaterThan(200);

      // paper order against the live book
      await page.click('nav.tabs button[data-tab="paper"]');
      await page.click('#tab-paper button.buy');
      await page.waitForFunction(() => document.querySelector('#tab-paper table button[data-close]') !== null, null, { timeout: 5000 });

      if (errors.length) console.log('SRVLOG ' + server.log.join('').split('\n').filter((l) => /HTTP 5/.test(l)).slice(-20).join('\n'));
      expect(errors, errors.join('\n')).toEqual([]);
      await page.context().close();
    });
  });
}

describe('Detector events reach the UI', () => {
  it('shows detector events with confidence and explanations in the journal', async () => {
    const { page } = await openApp({ width: 1280, height: 800 }, false);
    await page.click('nav.tabs button[data-tab="signals"]');
    await page.fill('#tab-signals input[type=number]', '0');
    await page.dispatchEvent('#tab-signals input[type=number]', 'change');
    await page.waitForFunction(() => document.querySelectorAll('#tab-signals tbody tr').length > 0, null, { timeout: 90_000 });
    const rows = await page.$$eval('#tab-signals tbody tr', (trs) => trs.map((t) => t.textContent ?? ''));
    console.log(`journal rows: ${rows.length}\n` + rows.slice(0, 8).join('\n'));
    await page.screenshot({ path: `${OUT}/desktop-signals-events.png` });
    expect(rows.length).toBeGreaterThan(0);
    await page.context().close();
  });
});

describe('History survives a restart (Supabase is the source of truth)', () => {
  it('restores events, heat tiles, gaps and archived L2 from Postgres with an empty local cache', async () => {
    // wait until at least one archive block is final and some events were persisted
    for (let i = 0; i < 60; i++) {
      const [{ n }] = await pg.sql`select count(*)::int as n from oft.archives where status = 'final'`;
      const [{ e }] = await pg.sql`select count(*)::int as e from oft.events`;
      if (n > 0 && e > 0) break;
      await wait(1000);
    }
    const before = (await (await fetch(`${BASE}/api/events?${q()}`)).json()) as { id: string }[];
    const [{ tiles }] = await pg.sql`select count(*)::int as tiles from oft.heat_tiles`;
    const [blk] = await pg.sql`select t0, t1 from oft.archives where status = 'final' order by t0 limit 1`;
    expect(before.length).toBeGreaterThan(0);
    expect(tiles).toBeGreaterThan(0);
    expect(blk).toBeTruthy();

    await server.stop();
    // restart while the exchange REST refuses instrument info (as with Binance 418 bans on shared cloud IPs):
    // recording must still start from the instrument parameters stored in Postgres
    venue.banned.add('/fapi/v1/exchangeInfo');
    server = await startServer({ venueUrl: venue.url, venueWs: venue.wsUrl, pgPort: pg.port, archiveUrl: arch.url });
    BASE = server.base;
    const inst = (await (await fetch(`${BASE}/api/instruments?source=binance-futures`)).json()) as { symbol: string }[];
    expect(inst.map((i) => i.symbol)).toContain('TESTUSDT');
    const after = (await (await fetch(`${BASE}/api/events?${q()}`)).json()) as { id: string }[];
    const ids = new Set(after.map((e) => e.id));
    expect(before.filter((e) => ids.has(e.id)).length).toBe(before.length);

    const heat = (await (await fetch(`${BASE}/api/heatmap?${q({ from: String(Date.now() - 10 * 60_000) })}`)).json()) as { cols: unknown[]; tiers: string[] };
    expect(heat.cols.length).toBeGreaterThan(0);

    // the downtime between the two processes is recorded as a gap (never interpolated)
    let gaps: { gaps: { reason: string; t0: number; t1: number | null }[]; coverage: unknown[] } = { gaps: [], coverage: [] };
    for (let i = 0; i < 30 && !gaps.gaps.some((g) => /сервис не работал/.test(g.reason)); i++) {
      await wait(1000);
      gaps = await (await fetch(`${BASE}/api/gaps?${q()}`)).json();
    }
    const down = gaps.gaps.find((g) => /сервис не работал/.test(g.reason));
    expect(server.log.join('')).toMatch(/using stored instrument parameters/);
    expect(down).toBeTruthy();
    expect(down!.t1! - down!.t0).toBeGreaterThan(0);
    expect(gaps.coverage.length).toBeGreaterThan(0);

    const t = Math.floor((Number(blk.t0) + Number(blk.t1)) / 2);
    const book = (await (await fetch(`${BASE}/api/archive/book?${q({ t: String(t) })}`)).json()) as { continuous: boolean; bids: unknown[]; asks: unknown[] };
    expect(book.continuous).toBe(true);
    expect(book.bids.length).toBeGreaterThan(0);
    expect(book.asks.length).toBeGreaterThan(0);

    // owner-only endpoints refuse anonymous writes
    const anon = await fetch(`${BASE}/api/paper/order`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    expect(anon.status).toBe(401);
    venue.banned.clear();
    const wl = (await (await fetch(`${BASE}/api/watchlist`)).json()) as { keys: string[]; rows: unknown[] };
    expect(wl.keys).toContain('binance-futures:TESTUSDT');
    const su = await fetch(`${BASE}/api/setups?${q()}`);
    expect(su.status).toBe(200);
    const st = (await (await fetch(`${BASE}/api/levels/static?${q()}`)).json()) as { levels: unknown[]; days: number };
    expect(Array.isArray(st.levels)).toBe(true);
    expect(st.levels.length).toBeLessThanOrEqual(4);
  });
});
