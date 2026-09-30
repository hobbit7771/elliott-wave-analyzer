# LLM intraday trader experiment: market picture -> local LLM decision -> simulated trade (Bybit taker fees).
# usage: python3 run.py PROMPT_VERSION dev|test [max_events]
import json, os, sys, time, hashlib, pickle, urllib.request, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from data import minute_bars, daily, events, atr, S
from prompts import SYSTEM
H = os.path.dirname(os.path.abspath(__file__))
COST = 0.0013          # round trip: 2 x 0.055 % taker + 2 bp slippage
MAX_HOLD = pd.Timedelta(hours=12)
SPLIT = pd.Timestamp('2026-08-27')
SYMS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
URL = os.environ.get('LLM', 'http://127.0.0.1:8090') + '/v1/chat/completions'

def load():
    p = f'{H}/cache.pkl'
    if os.path.exists(p): return pickle.load(open(p, 'rb'))
    D = {}
    for s in SYMS:
        m, dep = minute_bars(s); dd = daily(s, m)
        D[s] = (m, dep, dd, events(s, m, dd))
    pickle.dump(D, open(p, 'wb'))
    return D

def fp(x):
    return f'{x:.5g}' if x < 1000 else f'{x:.1f}'

def bars_text(b, avgv, now_px):
    rows = []
    for t, r in b.iterrows():
        v = r.bq + r.sq
        d = (r.bq - r.sq) / v * 100 if v else 0
        rows.append(f"{t:%H:%M} O{fp(r.o)} H{fp(r.h)} L{fp(r.l)} C{fp(r.c)} vol x{v / avgv:.1f} delta {d:+.0f}%")
    return '\n'.join(rows)

def picture(D, e):
    s = e['sym']; m, dep, dd, _ = D[s]; t = e['dec']
    past = m[m.index < t]
    px = past.c.iloc[-1]
    h1 = past.resample('1h').agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'bq': 'sum', 'sq': 'sum'}).dropna()
    m5 = past.resample('5min').agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'bq': 'sum', 'sq': 'sum'}).dropna()
    a1 = atr(h1).iloc[-2]
    d = dd[dd.index < t.normalize()]
    da = atr(d).iloc[-1]
    sma20 = d.c.iloc[-20:].mean()
    today = past[past.index >= t.normalize()]
    vol_today = (today.bq + today.sq).sum()
    cvd = (today.bq - today.sq).sum() / vol_today * 100 if vol_today else 0
    last30 = past.iloc[-30:]
    v30 = (last30.bq + last30.sq).sum()
    d30 = (last30.bq - last30.sq).sum() / v30 * 100 if v30 else 0
    avg1h = (h1.bq + h1.sq).iloc[-25:-1].mean(); avg5 = (m5.bq + m5.sq).iloc[-289:-1].mean()
    trend = 'UP' if d.c.iloc[-1] > sma20 and sma20 > d.c.iloc[-20:].iloc[:5].mean() else 'DOWN' if d.c.iloc[-1] < sma20 and sma20 < d.c.iloc[-20:].iloc[:5].mean() else 'SIDEWAYS'
    lvname = {'PDH': 'PREVIOUS-DAY HIGH', 'PDL': 'PREVIOUS-DAY LOW', 'ASIA_H': 'ASIA-SESSION HIGH (00-08 UTC)', 'ASIA_L': 'ASIA-SESSION LOW (00-08 UTC)'}[e['level']]
    lines = [f"COIN {s}  time {t:%Y-%m-%d %H:%M} UTC",
             f"EVENT: price touched the {lvname} {fp(e['price'])} at {e['touch']:%H:%M}. Price now {fp(px)} ({(px / e['price'] - 1) * 100:+.2f}% from the level).",
             f"DAILY: trend {trend}; close vs 20-day average {(d.c.iloc[-1] / sma20 - 1) * 100:+.1f}%; 20-day range {fp(d.l.iloc[-20:].min())}-{fp(d.h.iloc[-20:].max())}; daily ATR {da / px * 100:.1f}%.",
             "last 10 daily closes (oldest first): " + ' '.join(f"{x:+.1f}%" for x in (d.c.pct_change().iloc[-10:] * 100)),
             f"TODAY so far: open {fp(today.o.iloc[0])} high {fp(today.h.max())} low {fp(today.l.min())}; previous day high {fp(d.h.iloc[-1])} low {fp(d.l.iloc[-1])}.",
             f"1-hour ATR {fp(a1)} ({a1 / px * 100:.2f}%).",
             "1h bars (oldest first; vol = volume vs 24h average; delta = (aggressive buys - aggressive sells)/volume):",
             bars_text(h1.iloc[-12:], avg1h, px),
             "5m bars (oldest first):",
             bars_text(m5.iloc[-12:], avg5, px),
             f"ORDER FLOW: today's cumulative delta {cvd:+.1f}% of volume; last 30 min delta {d30:+.1f}%; large prints (>=250k USDT) last 30 min: buys {int(last30.bigb.sum())}, sells {int(last30.bigs.sum())}."]
    if dep is not None and t - pd.Timedelta(minutes=1) in dep.index:
        def imb(row, p):
            b = row[-p]; a = row[p]; return (b - a) / (b + a) * 100
        r0 = dep.loc[t - pd.Timedelta(minutes=1)]; r30 = dep.loc[t - pd.Timedelta(minutes=31)] if t - pd.Timedelta(minutes=31) in dep.index else r0
        lines.append(f"ORDER BOOK (resting limit orders): within 1%: bids {r0[-1.0] / 1e6:.0f}M vs asks {r0[1.0] / 1e6:.0f}M USDT (imbalance {imb(r0, 1.0):+.0f}%, 30 min ago {imb(r30, 1.0):+.0f}%); within 2%: {imb(r0, 2.0):+.0f}%; within 5%: {imb(r0, 5.0):+.0f}%.")
    if s != 'BTCUSDT':
        bm = D['BTCUSDT'][0]; bp = bm[bm.index < t].c
        lines.append(f"BTC: last 1h {(bp.iloc[-1] / bp.iloc[-61] - 1) * 100:+.2f}%, last 4h {(bp.iloc[-1] / bp.iloc[-241] - 1) * 100:+.2f}%.")
    feat = {'hi': 1 if e['level'].endswith('H') else -1, 'trend': {'UP': 1, 'DOWN': -1}.get(trend, 0), 'd30': d30 / 50, 'cvd': cvd / 20,
            'r1h': (px / h1.c.iloc[-2] - 1) / (a1 / px), 'r4h': (px / h1.c.iloc[-5] - 1) / (a1 / px) / 2, 'vol': np.log((m5.bq + m5.sq).iloc[-3:].mean() / avg5 + 1e-9),
            'dist': (px / e['price'] - 1) / (a1 / px), 'sma': (d.c.iloc[-1] / sma20 - 1) / (da / px)}
    PICT_FEAT.clear(); PICT_FEAT.update(feat)
    return '\n'.join(lines), px, a1

PICT_FEAT = {}

def outcome_text(D, e, px, a1):
    """What happened after a past event: move over 1 h and 4 h in 1h-ATR units and which rule won."""
    m = D[e['sym']][0]; w = m[(m.index >= e['dec'])]
    p1 = w.c.iloc[min(60, len(w) - 1)]; p4 = w.c.iloc[min(240, len(w) - 1)]
    hi4 = w.h.iloc[:240].max(); lo4 = w.l.iloc[:240].min()
    return f"after 1h {(p1 - px) / a1:+.1f} ATR, after 4h {(p4 - px) / a1:+.1f} ATR (4h max {(hi4 - px) / a1:+.1f}, min {(lo4 - px) / a1:+.1f} ATR)"

def simulate(D, e, side, stop, target):
    m = D[e['sym']][0]
    w = m[(m.index >= e['dec']) & (m.index < e['dec'] + MAX_HOLD)]
    entry = w.o.iloc[0]
    risk = abs(entry - stop) / entry
    if risk <= 0: return None
    exit_ = w.c.iloc[-1]; why = 'time'
    for _, r in w.iterrows():
        if side > 0:
            if r.l <= stop: exit_, why = stop, 'stop'; break
            if r.h >= target: exit_, why = target, 'target'; break
        else:
            if r.h >= stop: exit_, why = stop, 'stop'; break
            if r.l <= target: exit_, why = target, 'target'; break
    pnl = side * (exit_ / entry - 1) - COST
    return {'pnl': pnl, 'R': pnl / risk, 'why': why, 'entry': entry}

def rule(D, e, kind, px, a1):
    fade = -1 if e['level'].endswith('H') else 1
    side = fade if kind == 'fade' else -fade
    stop = px - side * a1; target = px + side * 2 * a1
    return side, stop, target

def ask(prompt):
    body = {'temperature': 0, 'max_tokens': 120, 'chat_template_kwargs': {'enable_thinking': False}, 'messages': [{'role': 'system', 'content': SYSTEM[VER]}, {'role': 'user', 'content': prompt}]}
    r = urllib.request.urlopen(urllib.request.Request(URL, data=json.dumps(body).encode(), headers={'content-type': 'application/json'}), timeout=600).read()
    return json.loads(r)['choices'][0]['message']['content']

def parse2(ans, px, a1, thr=0.6):
    import re
    ma = re.search(r'"action"\s*:\s*"(\w+)"', ans); mp = re.search(r'"p_win"\s*:\s*([0-9.]+)', ans)
    if not ma: return 0, None, None, 'unparseable'
    o = {'action': ma.group(1), 'p_win': mp.group(1) if mp else 0}
    act = str(o.get('action', 'SKIP')).upper()
    try: p = float(o.get('p_win', 0))
    except Exception: p = 0
    if act not in ('LONG', 'SHORT'): return 0, None, None, 'skip'
    if p < thr: return 0, None, None, 'low p'
    side = 1 if act == 'LONG' else -1
    return side, px - side * a1, px + side * 2 * a1, 'ok'

def parse(ans, px, a1):
    import re
    mm = re.search(r'\{[\s\S]*\}', ans)
    if not mm: return 0, None, None, 'unparseable'
    try: o = json.loads(mm.group(0))
    except Exception: return 0, None, None, 'bad json'
    act = str(o.get('action', 'SKIP')).upper()
    if act not in ('LONG', 'SHORT'): return 0, None, None, 'skip'
    side = 1 if act == 'LONG' else -1
    try: stop = float(o.get('stop')); target = float(o.get('target'))
    except Exception: return 0, None, None, 'no stop/target'
    # hard risk rules in code: stop on the right side, 0.25..3 x 1h ATR; reward 0.8..5 R
    dist = (px - stop) * side
    if dist <= 0: return 0, None, None, 'stop on wrong side'
    dist = min(max(dist, 0.25 * a1), 3 * a1); stop = px - side * dist
    rr = (target - px) * side / dist
    if rr <= 0: return 0, None, None, 'target on wrong side'
    rr = min(max(rr, 0.8), 5); target = px + side * rr * dist
    return side, stop, target, 'ok'

if __name__ == '__main__':
    VER = sys.argv[1]; USE_MEM = VER.startswith('m'); part = sys.argv[2]; nmax = int(sys.argv[3]) if len(sys.argv) > 3 else 10**9
    D = load()
    ev = sorted([e for s in SYMS for e in D[s][3]], key=lambda e: e['dec'])
    ev = [e for e in ev if (e['dec'] < SPLIT) == (part == 'dev') and e['dec'] > pd.Timestamp('2026-07-29')][:nmax]
    out = f"{H}/res_{VER}{os.environ.get('TAG', '')}_{part}.jsonl"
    done = {json.loads(l)['key'] for l in open(out)} if os.path.exists(out) else set()
    MEM = []  # (dec, features, one-line summary with outcome) of events whose 4h outcome is already known
    allev = sorted([e for s in SYMS for e in D[s][3]], key=lambda e: e['dec'])
    for e in ev:
        key = f"{e['sym']}|{e['dec']}|{e['level']}"
        if key in done: continue
        pic, px, a1 = picture(D, e)
        if USE_MEM:
            # memory: all earlier events (any coin) finished at least 4h before this decision
            for x in allev:
                if x['dec'] + pd.Timedelta(hours=4) > e['dec']: break
                if any(x is y[3] for y in MEM): continue
                p2, px2, a2 = picture(D, x); f2 = dict(PICT_FEAT)
                px_, a_ = px2, a2
                fd = rule(D, x, 'fade', px_, a_); bk = rule(D, x, 'breakout', px_, a_)
                fr = simulate(D, x, *fd); br = simulate(D, x, *bk)
                f2['_win'] = 'fade' if fr and fr['why'] == 'target' else 'breakout' if br and br['why'] == 'target' else 'neither'
                MEM.append((x['dec'], f2, f"{x['sym']} {x['dec']:%m-%d %H:%M} touched {x['level']}; trend {['DOWN','SIDEWAYS','UP'][f2['trend'] + 1]}, 30m delta {f2['d30'] * 50:+.0f}%, day CVD {f2['cvd'] * 20:+.0f}%, last 1h {f2['r1h']:+.1f} ATR -> " + outcome_text(D, x, px2, a2), x))
            pic, px, a1 = picture(D, e); f = dict(PICT_FEAT)
            if MEM:
                keys = list(f)
                dist = [sum((f[k] - mf[k]) ** 2 for k in keys if not k.startswith('_')) for _, mf, _, _ in MEM]
                K = 10 if VER >= 'm3' else 5
                idx = np.argsort(dist)[:K]
                near = [MEM[i][2] for i in idx[:5]]
                if VER >= 'm3':
                    from collections import Counter
                    c = Counter(MEM[i][1]['_win'] for i in idx)
                    pic += f"\nOF THE {len(idx)} MOST SIMILAR PAST TOUCHES: fade (trade back from the level, 2 ATR target) won {c['fade']}, breakout (trade through the level) won {c['breakout']}, neither reached target {c['neither']}."
                pic += '\nSIMILAR PAST SITUATIONS (most similar first) and what happened next:\n' + '\n'.join(near)
        rec = {'key': key, 'sym': e['sym'], 'level': e['level'], 'dec': str(e['dec'])}
        for k in ('fade', 'breakout'):
            sd, st, tg = rule(D, e, k, px, a1); rec[k] = simulate(D, e, sd, st, tg)
        rng = np.random.default_rng(int(hashlib.md5(key.encode()).hexdigest()[:8], 16))
        sd = int(rng.choice([-1, 1])); rec['random'] = simulate(D, e, sd, px - sd * a1, px + sd * 2 * a1)
        if VER != 'rules':
            t0 = time.time(); ans = ask(pic); rec['secs'] = round(time.time() - t0, 1)
            side, stop, target, why = (parse(ans, px, a1) if VER == 'v1' else parse2(ans, px, a1))
            rec['answer'] = ans; rec['parse'] = why
            rec['llm'] = simulate(D, e, side, stop, target) if side else None
            rec['side'] = side
        with open(out, 'a') as f: f.write(json.dumps(rec, default=str) + '\n')
        print(key, rec.get('parse', ''), rec.get('side', ''), (rec.get('llm') or {}).get('R', ''), rec.get('secs', ''), flush=True)
