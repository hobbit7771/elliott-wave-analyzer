# Five Fibonacci / candle-range setups from an Instagram post (monxa_i_ding), rules fixed before the run.
# 12 coins, 15m data 2020-01..2026-09 resampled to 1h / 4h / 1d. Halves: A < 2023-05-01, B >= 2023-05-01.
# Conservative fills: limit at the level (or the open if it gaps through), stop before target inside one bar,
# 0.075 % per side on entry and exit, results in R (risk = entry - stop).
import json, sys, numpy as np, pandas as pd
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'INJUSDT', 'SUIUSDT', 'UNIUSDT']
COST = 0.00075; SPLIT = pd.Timestamp('2023-05-01')
PIV = {'1h': 5, '4h': 3, '1d': 2}; WAIT = {'1h': 120, '4h': 60, '1d': 30}; HOLDMAX = {'1h': 400, '4h': 200, '1d': 100}
def bars(sym, tf):
    k = np.array(json.load(open(f'{S}/data5y/{sym}_15m.json')), float)
    d = pd.DataFrame(k[:, 1:5], columns=['o', 'h', 'l', 'c'], index=pd.to_datetime(k[:, 0], unit='ms'))
    rule = {'1h': 'h', '4h': '4h', '1d': 'D'}[tf]
    return d.resample(rule).agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last'}).dropna()
def atr(d, n=14):
    pc = d.c.shift(); tr = np.maximum(d.h - d.l, np.maximum((d.h - pc).abs(), (d.l - pc).abs())); return tr.rolling(n).mean().values
def swings(d, n):
    """confirmed fractal pivots: (index, confirm_index, price, +1 high / -1 low), alternating"""
    h, l = d.h.values, d.l.values; out = []
    for i in range(n, len(d) - n):
        if h[i] == h[i - n:i + n + 1].max(): out.append((i, i + n, h[i], 1))
        if l[i] == l[i - n:i + n + 1].min(): out.append((i, i + n, l[i], -1))
    out.sort(key=lambda x: (x[1], x[0])); alt = []
    for p in out:                                         # keep the more extreme of consecutive same-type pivots
        if alt and alt[-1][3] == p[3]:
            if (p[3] == 1 and p[2] > alt[-1][2]) or (p[3] == -1 and p[2] < alt[-1][2]): alt[-1] = p
        else: alt.append(p)
    return alt
def trade(d, start, side, entry, stop, target, wait, holdmax, cancel_px):
    """limit entry from bar start; cancel if price makes a new extreme beyond cancel_px before the fill"""
    o, h, l, c = d.o.values, d.h.values, d.l.values, d.c.values; n = len(d)
    for j in range(start, min(n, start + wait)):
        if side == 1:
            if h[j] > cancel_px and l[j] > entry: return None           # new high first: setup gone
            if l[j] <= entry:
                px = min(o[j], entry); break
        else:
            if l[j] < cancel_px and h[j] < entry: return None
            if h[j] >= entry:
                px = max(o[j], entry); break
    else: return None
    risk = (px - stop) * side
    if risk <= 0: return None
    for k in range(j, min(n, j + holdmax)):
        if side == 1:
            if l[k] <= stop: ex = min(o[k], stop) if k > j else stop; break
            if k > j and h[k] >= target: ex = max(o[k], target); break
        else:
            if h[k] >= stop: ex = max(o[k], stop) if k > j else stop; break
            if k > j and l[k] <= target: ex = min(o[k], target); break
    else: ex = c[min(n, j + holdmax) - 1]
    return (d.index[j], ((ex - px) * side - COST * (px + ex)) / risk)
# fib setups: fib price for level x on an up-leg L->H (0 at H, 1 at L); mirror for down-legs
SETUPS = {
    'POP 0.559 / stop 0.669 / tgt 0':    dict(e=0.559, s=0.669, t=0.0),
    'Monkey 0.63 / stop low / tgt -0.33': dict(e=0.63, s=1.0, t=-0.33, monkey=True),
    'Monkey tgt -0.66':                   dict(e=0.63, s=1.0, t=-0.66, monkey=True),
    'Monkey tgt -0.99':                   dict(e=0.63, s=1.0, t=-0.99, monkey=True),
    'OTE 0.705 / stop low / tgt 0':       dict(e=0.705, s=1.0, t=0.0),
    'Targets 0.5 / stop low / tgt -1':    dict(e=0.5, s=1.0, t=-1.0),
    'Targets 0.5 / stop low / tgt -2':    dict(e=0.5, s=1.0, t=-2.0),
}
CONTROL = [0.3, 0.4, 0.45, 0.5, 0.55, 0.559, 0.6, 0.618, 0.65, 0.705, 0.75, 0.786, 0.85]   # entry grid, stop low, target high
def fib_trades(d, tf):
    a = atr(d); sw = swings(d, PIV[tf]); out = []
    for p0, p1 in zip(sw, sw[1:]):
        i1, c1, x1, t1 = p1; i0, c0, x0, t0 = p0
        if not np.isfinite(a[i1]) or abs(x1 - x0) < 3 * a[i1]: continue
        side = 1 if t1 == 1 else -1                                # up-leg ends at a high: long the pullback
        H, L = (x1, x0) if side == 1 else (x1, x0)                 # H = leg end, L = leg start
        rng = H - L                                                # signed
        buf = 0.1 * a[i1] * side
        def lv(x, monkey=False):
            if monkey: U = rng / 0.96; return H + (0.03 - x) * U     # 0.99 at L, 0.03 at H
            return H - x * rng
        start = c1 + 1
        if start >= len(d): continue
        now = d.c.values[c1]
        for name, s in SETUPS.items():
            m = s.get('monkey', False); e = lv(s['e'], m); st = lv(s['s'], m) - (buf if s['s'] >= 1 else 0); tg = lv(s['t'], m)
            if (now - e) * side <= 0: continue                    # already through the entry at confirmation: skip
            r = trade(d, start, side, e, st, tg, WAIT[tf], HOLDMAX[tf], H)
            if r: out.append((name, side, *r))
        for x in CONTROL:
            e = lv(x); st = lv(1.0) - buf; tg = lv(0.0)
            if (now - e) * side <= 0: continue
            r = trade(d, start, side, e, st, tg, WAIT[tf], HOLDMAX[tf], H)
            if r: out.append((f'grid {x}', side, *r))
    return out
def crt_trades(d, tf):
    o, h, l, c = d.o.values, d.h.values, d.l.values, d.c.values; a = atr(d); out = []
    for i in range(20, len(d) - 2):
        R = h[i] - l[i]
        if not np.isfinite(a[i]) or R < 0.5 * a[i]: continue
        j = i + 1
        for side in (1, -1):
            if side == 1:
                depth = (l[i] - l[j]) / R; back = c[j] > l[i]
            else:
                depth = (h[j] - h[i]) / R; back = c[j] < h[i]
            if not back or depth <= 0: continue
            zone = 'CRT sweep -0.21..-0.4' if 0.21 <= depth <= 0.4 else ('CRT control: sweep < 0.21' if depth < 0.21 else None)
            if not zone: continue
            e = c[j]
            st = l[i] - 0.4 * R if side == 1 else h[i] + 0.4 * R
            for tgt, lab in ((1.5, 'tgt 1.5'), (2.6, 'tgt 2.6')):
                tg = l[i] + tgt * R if side == 1 else h[i] - tgt * R
                if (e - st) * side <= 0 or (tg - e) * side <= 0: continue
                # market entry at the close of the sweep candle: simulate from the next bar
                risk = (e - st) * side; ex = None
                for k in range(j + 1, min(len(d), j + 1 + HOLDMAX[tf])):
                    if (l[k] <= st) if side == 1 else (h[k] >= st): ex = min(o[k], st) if side == 1 else max(o[k], st); break
                    if (h[k] >= tg) if side == 1 else (l[k] <= tg): ex = max(o[k], tg) if side == 1 else min(o[k], tg); break
                if ex is None: ex = c[min(len(d), j + 1 + HOLDMAX[tf]) - 1]
                out.append((f'{zone} {lab}', side, d.index[j], ((ex - e) * side - COST * (e + ex)) / risk))
    return out
rows = []
for tf in ('1h', '4h', '1d'):
    for s in COINS:
        d = bars(s, tf)
        for name, side, t, r in fib_trades(d, tf) + crt_trades(d, tf): rows.append((tf, s, name, side, t, r))
    print('done', tf, flush=True)
T = pd.DataFrame(rows, columns=['tf', 'sym', 'setup', 'side', 't', 'R']); T.to_pickle(f'{S}/fibset/trades.pkl')
