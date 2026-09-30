# Independent check of every Gerchik trade of the engine (15m): (1) the fill is possible on that bar, (2) the trigger
# (БПУ1 touch / compression) exists on earlier bars, (3) the exit and R recomputed from raw bars with the same fees.
import json, numpy as np, pandas as pd
FM, FT, SL = 0.0002, 0.00055, 0.0002; MS = 15 * 60000; HOLD = 480
d = pd.DataFrame(json.load(open('rtf_15m.json'))); g = d[d.model.isin(['BOUNCE', 'BREAKOUT'])].copy()
bars = {}
def B(s):
    if s not in bars:
        a = np.array(json.load(open(f'data5y/{s}USDT_15m.json'))); bars[s] = (a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4])
    return bars[s]
bad_fill = bad_trig = 0; diffs = []; trig_miss = []
for _, x in g.iterrows():
    t, o, h, l, c = B(x.sym); i = int(np.searchsorted(t, x.t)); assert t[i] == x.t
    d_ = 1 if x.dir == 'LONG' else -1; risk = abs(x.entry - x.sl)
    # (1) fill: limit (bounce) needs the bar to trade at the price; stop (breakout) needs it to trade through
    if x.model == 'BOUNCE': ok = l[i] <= x.entry + 1e-12 if d_ > 0 else h[i] >= x.entry - 1e-12
    else: ok = h[i] >= x.entry - 1e-12 if d_ > 0 else l[i] <= x.entry + 1e-12
    bad_fill += not ok
    # (2) trigger on an earlier bar within the order validity: bounce = touch of level within the luft (L = 0.2*S = 0.2*risk)
    L = 0.2 * risk; lv = x.level; found = False
    rng = range(max(1, i - 48), i)
    for j in rng:
        if x.model == 'BOUNCE':
            if d_ > 0 and lv - L - 1e-9 <= l[j] <= lv + L + 1e-9 and c[j] > lv and c[j - 1] > lv + L - 1e-9: found = True
            if d_ < 0 and lv - L - 1e-9 <= h[j] <= lv + L + 1e-9 and c[j] < lv and c[j - 1] < lv - L + 1e-9: found = True
        else:
            k = j - 3
            if k < 1: continue
            w = slice(k, j + 1)
            if d_ > 0 and (c[w] < lv).all() and (np.diff(l[w]) >= 0).all(): found = True
            if d_ < 0 and (c[w] > lv).all() and (np.diff(h[w]) <= 0).all(): found = True
    if not found: bad_trig += 1; trig_miss.append((x.sym, pd.Timestamp(x.t, unit='ms'), x.model))
    # (3) exit
    ef = FM if x.model == 'BOUNCE' else FT; r = None
    for j in range(i, len(t)):
        barsIn = round((t[j] - x.t) / MS) + 1; fresh = j == i
        hs = l[j] <= x.sl if d_ > 0 else h[j] >= x.sl
        ht = (not fresh) and (h[j] >= x.tp if d_ > 0 else l[j] <= x.tp)
        if hs: r = (x.sl - x.entry) * d_ / risk - (ef * x.entry + (FT + SL) * x.entry) / risk; ct = t[j]; break
        if ht: r = 3 - (ef * x.entry + FM * x.entry) / risk; ct = t[j]; break
        if barsIn >= HOLD: r = (c[j] - x.entry) * d_ / risk - (ef * x.entry + (FT + SL) * x.entry) / risk; ct = t[j]; break
    diffs.append((r - x.r, ct == x.closed))
dr = np.array([a for a, _ in diffs]); same_close = np.mean([b for _, b in diffs])
print(f"Gerchik trades checked: {len(g)}")
print(f"(1) impossible fills: {bad_fill}")
print(f"(2) trigger not found on earlier bars: {bad_trig}  {trig_miss[:5]}")
print(f"(3) exit bar identical: {same_close * 100:.1f}%, |R difference| max {np.abs(dr).max():.2e}, mean {np.abs(dr).mean():.2e}")
print(f"    engine sum R {g.r.sum():+.1f}, recomputed {(g.r + dr).sum():+.1f}")
print('outcomes:', g.status.value_counts().to_dict(), '| avg risk as % of price', round((abs(g.entry - g.sl) / g.entry).mean() * 100, 2))
