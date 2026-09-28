// TEST-ONLY synthetic headlines for the news event-risk filter.
import { activeFlags, applyItem, classifyKeywords, findCoins, isRisk, liftFlags, parseLlm, scoreFlags, type NewsItem } from '../src/core/news.js';
import { SITE_TREND_PARAMS, TrendEngine } from '../src/core/levelEngine/trend.js';

const KNOWN = ['BTC', 'ETH', 'SOL', 'XRP', 'ONE', 'AI', 'ENA', 'ZRO'];
const T = Date.UTC(2026, 8, 28, 12);
const item = (title: string): NewsItem => ({ id: title, source: 'test', title, url: '', t: T });

describe('news event-risk filter', () => {
  it('finds tickers written in capitals and coin names, not common words', () => {
    expect(findCoins('Solana (SOL) and $ENA rally while ONE AI startup raises', KNOWN)).toEqual(['SOL', 'ENA']);
    expect(classifyKeywords(item('Ethereum developers delay upgrade'), KNOWN).coins).toEqual(['ETH']);
  });

  it('keyword fallback flags hacks and delistings, not listings', () => {
    const hack = classifyKeywords(item('ZRO bridge exploited, $40M drained'), KNOWN);
    expect(hack).toMatchObject({ coins: ['ZRO'], event: 'hack', sentiment: -2, by: 'keywords' });
    expect(isRisk(hack)).toBe(true);
    const del = classifyKeywords(item('Binance Will Delist ENA on 2026-10-05'), KNOWN);
    expect(del.event).toBe('delisting');
    expect(isRisk(del)).toBe(true);
    const list = classifyKeywords(item('Bybit will list SOL perpetual with 50x'), KNOWN);
    expect(list.event).toBe('listing');
    expect(isRisk(list)).toBe(false);
  });

  it('an exchange hack does not flag the large coins that were stolen or moved; the model can lift a keyword flag', () => {
    const moved = classifyKeywords(item('Bitget resumes Bitcoin withdrawals as hacker swaps ETH via THORChain'), KNOWN);
    expect(moved.coins).toEqual(['ETH', 'BTC']);
    expect(isRisk(moved)).toBe(false);
    expect(applyItem([], moved, T, () => 1)).toHaveLength(0);
    const kw = classifyKeywords(item('Hacker moves ZRO and ENA stolen from exchange'), KNOWN);
    const f = applyItem([], kw, T, () => 1);
    expect(f.map((x) => x.coin).sort()).toEqual(['ENA', 'ZRO']);
    const llm = parseLlm(kw, '{"coins":["ZRO"],"event":"hack","sentiment":-2,"severity":3}', KNOWN)!;
    expect(liftFlags(f, llm, T + 600_000)).toBe(1);
    expect(activeFlags(f, T + 600_001).map((x) => x.coin)).toEqual(['ZRO']);
    expect(f.find((x) => x.coin === 'ENA')!.lifted).toBe(true);
  });

  it('parses the model answer, keeps only known tickers, rejects prose', () => {
    const c = parseLlm(item('x'), 'Sure! {"coins":["eth","FOO","SOLUSDT"],"event":"regulatory","sentiment":-2,"severity":2}', KNOWN)!;
    expect(c).toMatchObject({ coins: ['ETH', 'SOL'], event: 'regulatory', sentiment: -2, severity: 2, by: 'llm' });
    expect(isRisk(c)).toBe(true);
    expect(parseLlm(item('x'), 'I cannot tell.', KNOWN)).toBeNull();
    expect(isRisk({ ...c, severity: 1 })).toBe(false); // minor regulatory noise is not a risk
  });

  it('flags for 72 h, extends instead of duplicating, scores the coin against BTC after 1 and 3 days', () => {
    let px: Record<string, number> = { ZRO: 2, BTC: 100 };
    const hack = classifyKeywords(item('ZRO exploited'), KNOWN);
    let f = applyItem([], hack, T, (c) => px[c]);
    f = applyItem(f, { ...hack, id: 'again', t: T + 3600_000 }, T + 3600_000, (c) => px[c]);
    expect(f).toHaveLength(1);
    expect(activeFlags(f, T + 72 * 3600_000)).toHaveLength(1);
    expect(activeFlags(f, T + 74 * 3600_000)).toHaveLength(0);
    px = { ZRO: 1.6, BTC: 110 };
    scoreFlags(f, T + 86_400_000 + 1, (c) => px[c]);
    expect(f[0].r1).toBeCloseTo(-0.2 - 0.1, 9);
    expect(f[0].r3).toBeUndefined();
  });

  it('the trend model places no long order for a coin at event risk and cancels a pending one', () => {
    const DAY = 86_400_000;
    const flat = Array.from({ length: 60 }, (_, i) => ({ t: T - (60 - i) * DAY, o: 100, h: 101, l: 99, c: 100, v: 1, bv: 0 }));
    const up = Array.from({ length: 80 }, (_, i) => ({ t: T - (80 - i) * DAY, o: 100 + i, h: 101 + i, l: 99 + i, c: 100.5 + i, v: 1, bv: 0 }));
    const day0 = Math.floor(T / DAY) * DAY;
    const e = new TrendEngine({ symbol: 'ZROUSDT', exchange: 'x', tick: 0.01 }, flat, SITE_TREND_PARAMS, up);
    e.step({ t: day0, o: 100, h: 100.2, l: 99.8, c: 100, v: 1, bv: 0 });
    expect(e.workingOrders()).toHaveLength(1);
    e.setEventRisk('hack: ZRO exploited');
    expect(e.workingOrders()).toHaveLength(0);
    expect(e.skipped).toMatch(/событие/);
    e.step({ t: day0 + 15 * 60_000, o: 100, h: 102, l: 99.8, c: 101.5, v: 1, bv: 0 }); // price trades through the old order level
    expect(e.setups).toHaveLength(0);
  });
});
