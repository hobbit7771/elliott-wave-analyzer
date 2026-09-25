// End-to-end: real server process + real Chromium, fed by the local test venue.
// Checks that every tab renders live data, markers appear, and layouts fit iPhone and desktop.
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdirSync, rmSync } from 'node:fs';
import { chromium, type Browser, type Page } from 'playwright-core';
import { freePort, startTestVenue, type TestVenue } from './testVenue.js';

let PORT = 0;
let BASE = '';
const OUT = 'test-results';
let venue: TestVenue;
let server: ChildProcess;
let browser: Browser;
const serverLog: string[] = [];

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  PORT = await freePort();
  BASE = `http://127.0.0.1:${PORT}`;
  rmSync(`/tmp/oft-e2e-${PORT}.sqlite`, { force: true });
  venue = await startTestVenue();
  server = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', 'dist/server/index.js'], {
    env: {
      ...process.env,
      NODE_ENV: 'test',
      PORT: String(PORT),
      DB_PATH: `/tmp/oft-e2e-${PORT}.sqlite`,
      BINANCE_FUTURES_REST: venue.url,
      BINANCE_FUTURES_WS: venue.wsUrl,
      DEFAULT_SYMBOLS: 'binance-futures:TESTUSDT',
      BACKFILL_MINUTES: '1',
    },
  });
  server.stdout?.on('data', (d) => serverLog.push(String(d)));
  server.stderr?.on('data', (d) => serverLog.push(String(d)));
  let up = false;
  for (let i = 0; i < 100 && !up; i++) {
    try {
      up = (await fetch(BASE + '/api/health')).ok;
    } catch {
      /* not up yet */
    }
    if (!up) await wait(200);
  }
  if (!up) throw new Error('server did not start:\n' + serverLog.join(''));
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  // let the server record some heatmap columns / trades first
  await wait(8000);
});

afterAll(async () => {
  await browser?.close();
  if (server && server.exitCode === null) {
    const exited = new Promise((r) => server.once('exit', r));
    server.kill('SIGTERM');
    await exited;
  }
  await venue?.close();
});

async function openApp(viewport: { width: number; height: number }, mobile: boolean): Promise<{ page: Page; errors: string[] }> {
  const ctx = await browser.newContext({ viewport, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: mobile ? 3 : 1 });
  await ctx.addInitScript(() => localStorage.setItem('oft:instrument', JSON.stringify({ source: 'binance-futures', symbol: 'TESTUSDT' })));
  const page = await ctx.newPage();
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => m.type() === 'error' && errors.push('console: ' + m.text()));
  await page.goto(BASE + '/#chart');
  await page.waitForFunction(() => document.querySelector('.badge')?.textContent === 'Connected', null, { timeout: 30_000 });
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

      for (const tab of ['heatmap', 'dom', 'footprint', 'profile', 'signals', 'paper', 'alerts', 'sources']) {
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
      const cols = Number((await page.textContent('#tab-heatmap .toolbar .muted'))?.match(/(\d+) columns/)?.[1] ?? 0);
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
