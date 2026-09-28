// News and exchange announcements -> event-risk flags per coin (src/core/news.ts). Every 5 minutes: RSS of large crypto
// media + Bybit and Binance delisting announcements; new headlines are classified by the local LLM (llama.cpp server,
// OpenAI-compatible API at LLM_URL) or, when it is unavailable, by keyword rules. State in oft.settings 'news:state'.
import type { Repo } from '../persist/repo.js';
import { LLM_SYSTEM, activeFlags, applyItem, classifyKeywords, liftFlags, parseLlm, scoreFlags, type Classified, type NewsItem, type RiskFlag } from '../../core/news.js';

const KEY = 'news:state';
const RSS = [
  { source: 'CoinDesk', url: 'https://www.coindesk.com/arc/outboundfeeds/rss/' },
  { source: 'Cointelegraph', url: 'https://cointelegraph.com/rss' },
  { source: 'Decrypt', url: 'https://decrypt.co/feed' },
  { source: 'The Block', url: 'https://www.theblock.co/rss.xml' },
];
const MAX_AGE = 3 * 86_400_000; // older items are not classified (they would flag stale events)

interface State {
  items: Classified[];
  flags: RiskFlag[];
  seen: string[];
}

export class NewsService {
  private st: State | null = null;
  private busy = false;
  private known: string[] = [];
  private knownAt = 0;
  private prices: Record<string, number> = {};
  llm = { ok: false, model: '', error: '', lastAt: 0, classified: 0 };
  sources: Record<string, { ok: boolean; n: number; error: string; at: number }> = {};
  error = '';

  constructor(
    private deps: {
      repo: () => Repo | null;
      log: (m: string) => void;
      /** USDT perpetual symbols (e.g. BTCUSDT) of the exchange */
      symbols: () => Promise<string[]>;
      /** last prices of USDT perpetuals by symbol */
      prices: () => Promise<Record<string, number>>;
      llmUrl?: string;
    },
  ) {}

  /** Coins at event risk right now: coin (ticker without USDT) -> reason. */
  blocked(): Map<string, string> {
    const now = Date.now();
    return new Map(activeFlags(this.st?.flags ?? [], now).map((f) => [f.coin, f.reason]));
  }

  private price = (coin: string): number | undefined => this.prices[coin + 'USDT'] ?? this.prices['1000' + coin + 'USDT'];

  async tick(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const repo = this.deps.repo();
      if (!this.st) this.st = (repo ? await repo.getSetting<State>(KEY).catch(() => undefined) : undefined) ?? { items: [], flags: [], seen: [] };
      const now = Date.now();
      if (!this.known.length || now - this.knownAt > 3600_000) {
        const syms = await this.deps.symbols().catch(() => [] as string[]);
        if (syms.length) {
          this.known = [...new Set(syms.map((s) => s.replace(/USDT$/, '').replace(/^1000+/, '')))];
          this.knownAt = now;
        }
      }
      this.prices = await this.deps.prices().catch(() => this.prices);
      const fresh = (await this.collect()).filter((x) => !this.st!.seen.includes(x.id) && now - x.t < MAX_AGE).sort((a, b) => a.t - b.t);
      let done = 0;
      for (const it of fresh) {
        if (done >= 12) break; // the local model is slow: at most 12 headlines per 5-minute tick
        const c = await this.classify(it);
        done++;
        this.st.seen.push(it.id);
        this.st.items.push(c);
        this.st.flags = applyItem(this.st.flags, c, now, this.price);
      }
      // spare model time: re-read recent headlines the keyword rules labelled (model was down), newest first;
      // flags the model does not confirm are lifted
      const redo = this.st.items.filter((x) => x.by === 'keywords' && now - x.t < MAX_AGE).reverse();
      for (const it of redo) {
        if (done >= 12 || !this.llm.ok) break;
        const c = await this.classify(it);
        done++;
        if (c.by !== 'llm') break;
        this.st.items[this.st.items.indexOf(it)] = c;
        liftFlags(this.st.flags, c, now);
        this.st.flags = applyItem(this.st.flags, c, now, this.price);
      }
      scoreFlags(this.st.flags, now, this.price);
      this.st.items = this.st.items.slice(-300);
      this.st.flags = this.st.flags.filter((f) => now - f.since < 60 * 86_400_000).slice(-500);
      this.st.seen = this.st.seen.slice(-3000);
      if (repo) await repo.setSetting(KEY, this.st).catch((e) => this.deps.log(`[news] state save failed: ${(e as Error).message}`));
      this.error = '';
    } catch (e) {
      this.error = (e as Error).message.slice(0, 200);
      this.deps.log(`[news] update failed: ${this.error}`);
    } finally {
      this.busy = false;
    }
  }

  private async text(url: string): Promise<string> {
    const r = await fetch(url, { headers: { 'user-agent': 'Mozilla/5.0 (OrderFlow Terminal news reader)', accept: '*/*' }, signal: AbortSignal.timeout(15_000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }

  private async collect(): Promise<NewsItem[]> {
    const out: NewsItem[] = [];
    const note = (src: string, ok: boolean, n: number, error = '') => (this.sources[src] = { ok, n, error, at: Date.now() });
    for (const f of RSS) {
      try {
        const xml = await this.text(f.url);
        const items = [...xml.matchAll(/<item[\s>][\s\S]*?<\/item>/g)].map((m) => m[0]);
        const pick = (x: string, tag: string) => (x.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`))?.[1] ?? '').replace(/<!\[CDATA\[|\]\]>/g, '').replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&#039;|&#39;/g, "'").replace(/&quot;/g, '"').trim();
        let n = 0;
        for (const x of items.slice(0, 30)) {
          const title = pick(x, 'title');
          const url = pick(x, 'link');
          const t = Date.parse(pick(x, 'pubDate')) || Date.now();
          if (title) {
            out.push({ id: url || `${f.source}:${title}`, source: f.source, title, url, t });
            n++;
          }
        }
        note(f.source, true, n);
      } catch (e) {
        note(f.source, false, 0, (e as Error).message.slice(0, 80));
      }
    }
    try {
      const j = JSON.parse(await this.text('https://api.bybit.com/v5/announcements/index?locale=en-US&type=delistings&limit=20')) as { result?: { list?: { title: string; url: string; publishTime: number }[] } };
      const list = j.result?.list ?? [];
      for (const a of list) out.push({ id: a.url || `bybit:${a.title}`, source: 'Bybit', title: a.title, url: a.url, t: +a.publishTime || Date.now() });
      note('Bybit', true, list.length);
    } catch (e) {
      note('Bybit', false, 0, (e as Error).message.slice(0, 80));
    }
    try {
      const j = JSON.parse(await this.text('https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=161&pageNo=1&pageSize=20')) as { data?: { catalogs?: { articles?: { code: string; title: string; releaseDate: number }[] }[] } };
      const list = j.data?.catalogs?.[0]?.articles ?? [];
      for (const a of list) out.push({ id: `binance:${a.code}`, source: 'Binance', title: a.title, url: `https://www.binance.com/en/support/announcement/${a.code}`, t: +a.releaseDate || Date.now() });
      note('Binance', true, list.length);
    } catch (e) {
      note('Binance', false, 0, (e as Error).message.slice(0, 80));
    }
    return out;
  }

  private async classify(it: NewsItem): Promise<Classified> {
    const url = this.deps.llmUrl ?? process.env.LLM_URL ?? '';
    if (url) {
      try {
        const r = await fetch(url.replace(/\/$/, '') + '/v1/chat/completions', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            temperature: 0,
            max_tokens: 120,
            messages: [
              { role: 'system', content: LLM_SYSTEM },
              { role: 'user', content: `Headline (${it.source}): ${it.title}` },
            ],
          }),
          signal: AbortSignal.timeout(90_000),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as { model?: string; choices?: { message?: { content?: string } }[] };
        const c = parseLlm(it, j.choices?.[0]?.message?.content ?? '', this.known);
        if (c) {
          this.llm = { ok: true, model: j.model ?? 'local', error: '', lastAt: Date.now(), classified: this.llm.classified + 1 };
          return c;
        }
        this.llm.error = 'unparseable answer';
      } catch (e) {
        this.llm = { ...this.llm, ok: false, error: (e as Error).message.slice(0, 120) };
      }
    }
    return classifyKeywords(it, this.known);
  }

  view(): unknown {
    const st = this.st;
    if (!st) return { status: 'loading', error: this.error };
    const now = Date.now();
    const real = st.flags.filter((f) => !f.lifted);
    const scored = real.filter((f) => f.r3 !== undefined);
    const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN);
    return {
      status: 'ok',
      error: this.error,
      llm: this.llm,
      sources: this.sources,
      known: this.known.length,
      active: activeFlags(st.flags, now),
      journal: {
        flags: real.length,
        lifted: st.flags.length - real.length,
        scored1: real.filter((f) => f.r1 !== undefined).length,
        avgR1: avg(real.filter((f) => f.r1 !== undefined).map((f) => f.r1!)),
        scored3: scored.length,
        avgR3: avg(scored.map((f) => f.r3!)),
        recent: real.slice(-30).reverse(),
      },
      items: st.items.slice(-80).reverse(),
    };
  }
}
