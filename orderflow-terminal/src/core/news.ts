// News and exchange announcements -> per-coin event-risk flags (paper strategies stay out of coins with a serious
// negative event: hack/exploit, delisting, regulatory action, large token unlock ...).
// Why (README round 9): price and candle data gave no stable edge; the largest single-coin losses come from events the
// strategies cannot see. A local LLM (llama.cpp on the server) classifies each headline; a keyword classifier is the
// fallback when the model is unavailable. The value of the filter is measured FORWARD (journal of flagged coins vs the
// market after 1 and 3 days) — an LLM backtest on old news is biased by what the model saw in training.

export type EventType = 'hack' | 'delisting' | 'unlock' | 'regulatory' | 'listing' | 'partnership' | 'macro' | 'other';

export interface NewsItem {
  id: string; // stable id (link or source + title)
  source: string;
  title: string;
  url: string;
  t: number; // publication time (ms)
}

export interface Classified extends NewsItem {
  coins: string[]; // tickers, upper case, without USDT
  event: EventType;
  sentiment: number; // −2 … +2
  severity: number; // 0 … 3
  by: 'llm' | 'keywords';
}

export interface RiskFlag {
  coin: string;
  since: number;
  until: number;
  reason: string;
  itemId: string;
  price?: number; // price when flagged (for the forward journal)
  market?: number; // BTC price when flagged
  r1?: number; // coin return minus BTC return after 1 day
  r3?: number; // ... after 3 days
  lifted?: boolean; // the model re-read the headline and found no risk for this coin: flag ended early, not in the journal
}

export const FLAG_HOURS = 72;
const NEG_EVENTS: EventType[] = ['hack', 'delisting', 'regulatory', 'unlock'];

// Large coins that hack stories mention as the STOLEN or MOVED asset ("hacker swaps ETH", "$83M in stolen XRP"): an
// exchange or bridge hack is not a risk to them. Seen live on 28.09.2026 (Bitget hack flagged BTC, ETH, XRP).
const CARRIERS = new Set(['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'TRX', 'LTC', 'USDT', 'USDC', 'DAI']);

/** Coins a classified item puts at event risk (serious negative events only); [] if none. */
export function riskCoins(c: Classified): string[] {
  const coins = c.event === 'hack' ? c.coins.filter((x) => !CARRIERS.has(x)) : c.coins;
  if (!coins.length) return [];
  if (c.event === 'delisting' || c.event === 'hack') return c.sentiment <= 0 ? coins : [];
  return NEG_EVENTS.includes(c.event) && c.sentiment <= -1 && c.severity >= 2 ? coins : [];
}

export const isRisk = (c: Classified): boolean => riskCoins(c).length > 0;

const KW: [EventType, RegExp, number, number][] = [
  // [event, pattern, sentiment, severity]
  ['hack', /\b(hack(ed|er|ers)?|exploit(ed)?|drain(ed)?|stolen|breach|rug ?pull|attack(er|ed)?)\b/i, -2, 3],
  ['delisting', /\b(delist(s|ed|ing)?|will (be )?remove|cease trading|termination of trading)\b/i, -2, 3],
  ['regulatory', /\b(sec (sues|charges|lawsuit)|lawsuit|sued|indict(ed|ment)|ban(s|ned)?|sanction(s|ed)?|investigat(ion|ing))\b/i, -1, 2],
  ['unlock', /\btoken unlock|unlocks? \$?\d/i, -1, 2],
  ['listing', /\b(will list|lists|listing|launch(es)? (spot|perpetual|futures))\b/i, 1, 1],
  ['partnership', /\b(partner(s|ship)?|integrat(es|ion))\b/i, 1, 1],
];

// all-caps words in headlines that are not coins even when a ticker of that name exists
const STOP = new Set('AI US USA SEC ETF ETFS CEO CTO USD EU UK IT ME ONE OK IPO CEX DEX NFT NFTS DAO APY APR TVL GDP CPI FED FOMC ATH OTC KYC AML API UN IMF ECB BOJ PM AM NEW TOP BIG ALL'.split(' '));

/** Tickers mentioned in a headline among the known ones: upper-case tokens as written ("BTC", "$SOL", "(ETH)"). */
export function findCoins(text: string, known: readonly string[]): string[] {
  const knownSet = new Set(known);
  const out = new Set<string>();
  for (const m of text.matchAll(/\$?\b([A-Z][A-Z0-9]{1,14})\b/g)) if (knownSet.has(m[1]) && !STOP.has(m[1])) out.add(m[1]);
  return [...out];
}

/** Common coin names -> tickers for headlines that name the coin in words. */
export const COIN_NAMES: Record<string, string> = {
  bitcoin: 'BTC', ethereum: 'ETH', ether: 'ETH', solana: 'SOL', ripple: 'XRP', dogecoin: 'DOGE', cardano: 'ADA', chainlink: 'LINK', avalanche: 'AVAX',
  polkadot: 'DOT', toncoin: 'TON', uniswap: 'UNI', litecoin: 'LTC', tron: 'TRX', hyperliquid: 'HYPE', aave: 'AAVE', sui: 'SUI', aptos: 'APT',
  arbitrum: 'ARB', optimism: 'OP', near: 'NEAR', cosmos: 'ATOM', filecoin: 'FIL', injective: 'INJ', pepe: 'PEPE', shiba: 'SHIB', stellar: 'XLM',
};

/** Fallback classifier (no model): keyword rules + ticker matching. Coin names (e.g. "Bitcoin") map through `names`. */
export function classifyKeywords(item: NewsItem, known: readonly string[], names: Record<string, string> = COIN_NAMES): Classified {
  const coins = new Set(findCoins(item.title, known));
  for (const [name, tick] of Object.entries(names)) if (known.includes(tick) && new RegExp(`\\b${name}\\b`, 'i').test(item.title)) coins.add(tick);
  return classifyWith(item, [...coins]);
}

function classifyWith(item: NewsItem, coins: string[]): Classified {
  for (const [event, re, sentiment, severity] of KW) if (re.test(item.title)) return { ...item, coins, event, sentiment, severity, by: 'keywords' };
  return { ...item, coins, event: 'other', sentiment: 0, severity: 0, by: 'keywords' };
}

export const LLM_SYSTEM =
  'You classify crypto news headlines for a risk filter. Answer with ONE JSON object only, no prose: ' +
  '{"coins":["TICKER",...],"event":"hack|delisting|unlock|regulatory|listing|partnership|macro|other","sentiment":-2..2,"severity":0..3}. ' +
  'coins: tickers of the cryptocurrencies whose OWN project, chain, token or issuer the headline is about (e.g. "Solana outage" -> SOL). ' +
  'Do NOT list assets that were only stolen, moved, swapped or mentioned (an exchange hack where the hacker moves ETH is not about ETH); ' +
  '[] if none, if the subject is an exchange or company without its own token here, or only the market in general. ' +
  'sentiment: expected effect on those coins, -2 very negative ... 2 very positive. severity: 0 noise, 1 minor, 2 material, 3 critical (hack, delisting, exchange collapse).';

/** Parse the model's answer; null if it is not a usable JSON object. Tickers are filtered to the known ones. */
export function parseLlm(item: NewsItem, answer: string, known: readonly string[]): Classified | null {
  const m = answer.match(/\{[\s\S]*\}/);
  if (!m) return null;
  let o: { coins?: unknown; event?: unknown; sentiment?: unknown; severity?: unknown };
  try {
    o = JSON.parse(m[0]);
  } catch {
    return null;
  }
  const events: EventType[] = ['hack', 'delisting', 'unlock', 'regulatory', 'listing', 'partnership', 'macro', 'other'];
  const event = events.includes(o.event as EventType) ? (o.event as EventType) : 'other';
  const knownSet = new Set(known);
  const coins = Array.isArray(o.coins) ? [...new Set(o.coins.map((c) => String(c).toUpperCase().replace(/USDT$/, '').replace(/[^A-Z0-9]/g, '')).filter((c) => knownSet.has(c)))] : [];
  const clamp = (x: unknown, lo: number, hi: number) => Math.max(lo, Math.min(hi, Math.round(Number(x) || 0)));
  return { ...item, coins, event, sentiment: clamp(o.sentiment, -2, 2), severity: clamp(o.severity, 0, 3), by: 'llm' };
}

/** Add flags for a risky item (one per coin; an existing active flag is extended). */
export function applyItem(flags: RiskFlag[], c: Classified, now: number, price: (coin: string) => number | undefined): RiskFlag[] {
  const coins = riskCoins(c);
  if (!coins.length) return flags;
  const until = Math.max(now, c.t) + FLAG_HOURS * 3600_000;
  const out = [...flags];
  for (const coin of coins) {
    const active = out.find((f) => f.coin === coin && f.until > now);
    if (active) {
      active.until = Math.max(active.until, until);
      continue;
    }
    out.push({ coin, since: now, until, reason: `${c.event}: ${c.title.slice(0, 140)}`, itemId: c.id, price: price(coin), market: price('BTC') });
  }
  return out;
}

/**
 * The item was re-classified (the model re-read a headline the keyword rules had labelled): end its active flags on the
 * coins the new reading does not put at risk. Lifted flags leave the forward journal (they were false alarms).
 */
export function liftFlags(flags: RiskFlag[], c: Classified, now: number): number {
  const keep = new Set(riskCoins(c));
  let n = 0;
  for (const f of flags) {
    if (f.itemId !== c.id || f.until <= now || keep.has(f.coin)) continue;
    f.until = now;
    f.lifted = true;
    n++;
  }
  return n;
}

export const activeFlags = (flags: readonly RiskFlag[], now: number): RiskFlag[] => flags.filter((f) => f.until > now);

/** Fill the forward journal: coin return minus BTC return 1 and 3 days after the flag. */
export function scoreFlags(flags: RiskFlag[], now: number, price: (coin: string) => number | undefined): void {
  for (const f of flags) {
    if (!(f.price && f.market)) continue;
    const p = price(f.coin);
    const m = price('BTC');
    if (!(p && m)) continue;
    const rel = p / f.price - 1 - (m / f.market - 1);
    if (f.r1 === undefined && now >= f.since + 86_400_000) f.r1 = rel;
    if (f.r3 === undefined && now >= f.since + 3 * 86_400_000) f.r3 = rel;
  }
}
