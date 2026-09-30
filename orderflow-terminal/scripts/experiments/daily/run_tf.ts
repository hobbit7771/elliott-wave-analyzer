// Site engine on 5+ years at any execution timeframe with parameter overrides (env CFG = JSON):
// { tf: '15m'|'1h'|'1d', g: {GerchikParams overrides}, tr: {TrendParams overrides}, noFunding, noMarket, until, out }
import { readFileSync, existsSync, writeFileSync } from 'node:fs';
import { SiteEngine, SITE_PARAMS } from '/home/user/elliott-wave-analyzer/orderflow-terminal/src/core/levelEngine/siteEngine.ts';
const cfg = JSON.parse(process.env.CFG ?? '{}');
const D = 'data5y', DAY = 86400000, START = Date.UTC(2021, 5, 1);
const TFMS: Record<string, number> = { '15m': 15 * 60000, '1h': 3600000, '1d': DAY };
const tf = cfg.tf ?? '15m', ms = TFMS[tf], k = ms / TFMS['15m'];
const toC = (r: number[]) => ({ t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5], bv: 0 });
const load = (f: string) => (JSON.parse(readFileSync(`${D}/${f}`, 'utf8')) as number[][]).map(toC);
function agg(b: any[], step: number) {
  const out: any[] = []; let cur: any = null;
  for (const x of b) { const t = Math.floor(x.t / step) * step;
    if (!cur || cur.t !== t) { if (cur) out.push(cur); cur = { ...x, t }; } else { cur.h = Math.max(cur.h, x.h); cur.l = Math.min(cur.l, x.l); cur.c = x.c; cur.v += x.v; } }
  if (cur) out.push(cur); return out;
}
// time-equivalent bar windows (the site values are for 15m bars); squeeze range scales with sqrt(time)
const g0 = SITE_PARAMS.gerchik;
const gScaled = { ...g0, ltfMs: ms, bpuWindow: Math.max(1, Math.round(g0.bpuWindow / k)), cooldownBars: Math.max(1, Math.round(g0.cooldownBars / k)),
  maxHoldBars: Math.max(1, Math.round(g0.maxHoldBars / k)), squeezeRangeAtr: g0.squeezeRangeAtr * Math.sqrt(k), fbReturnBars: Math.max(1, Math.round(g0.fbReturnBars / Math.sqrt(k))) };
const P = { gerchik: { ...gScaled, ...(cfg.g ?? {}) }, trend: { ...SITE_PARAMS.trend, ltfMs: ms, ...(cfg.tr ?? {}) } };
const btc = load('BTCUSDT_1d.json');
const out: any[] = [];
for (const s of (cfg.syms ?? 'BTC ETH SOL XRP DOGE BNB ADA LINK AVAX SUI UNI INJ'.split(' '))) {
  const d1 = load(`${s}USDT_1d.json`);
  const t0 = Math.max(START, Math.floor((d1[0].t + 120 * DAY) / DAY) * DAY);
  let bars = tf === '1d' ? d1.filter((x) => x.t >= t0) : load(`${s}USDT_15m.json`).filter((x) => x.t >= t0);
  if (tf === '1h') bars = agg(bars, ms);
  if (cfg.until) bars = bars.filter((x) => x.t < cfg.until);
  const daily = d1.filter((d) => d.t < t0);
  const fp = `fund/${s}.json`;
  const f = !cfg.noFunding && existsSync(fp) ? (JSON.parse(readFileSync(fp, 'utf8')) as number[][]).map(([d, v]) => ({ t: d * DAY, rate: v / 1e6 })) : [];
  const e = new SiteEngine({ symbol: s + 'USDT', exchange: 'x', tick: bars[0].c * 1e-5 }, daily, P, cfg.noMarket ? [] : btc, f);
  for (const b of bars) e.step(b);
  for (const x of e.setups) if (x.outcome.status !== 'open')
    out.push({ sym: s, t: x.t, model: x.reasons[1], dir: x.direction, r: x.outcome.r, status: x.outcome.status, closed: x.outcome.closedAt, entry: x.entry, sl: x.sl, tp: x.tp, level: x.level, trig: x.trigger.slice(0, 60) });
}
writeFileSync(cfg.out ?? 'rtf.json', JSON.stringify(out));
