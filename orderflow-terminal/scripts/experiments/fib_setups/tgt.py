# What drives "Ценовые цели": the 0.5 entry or the far target? Grid of entry x target on 4h and 1d, halves A/B,
# longs and shorts separately, plus a no-pullback control (market entry at swing confirmation, same stop and targets).
import sys, numpy as np, pandas as pd
sys.argv = ['x']; exec(open(f'/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/fibset/fibset.py').read().split('rows = []')[0])
rows = []
for tf in ('4h', '1d'):
    for s in COINS:
        d = bars(s, tf); a = atr(d); sw = swings(d, PIV[tf])
        for p0, p1 in zip(sw, sw[1:]):
            i1, c1, x1, t1 = p1; i0, c0, x0, t0 = p0
            if not np.isfinite(a[i1]) or abs(x1 - x0) < 3 * a[i1] or c1 + 1 >= len(d): continue
            side = 1 if t1 == 1 else -1; H, L = x1, x0; rng = H - L; buf = 0.1 * a[i1] * side
            lv = lambda x: H - x * rng; now = d.c.values[c1]
            for tg in (0.0, -1.0, -2.0, -3.0):
                for x in (0.3, 0.382, 0.5, 0.618, 0.7):
                    e = lv(x)
                    if (now - e) * side <= 0: continue
                    r = trade(d, c1 + 1, side, e, lv(1.0) - buf, lv(tg), WAIT[tf], HOLDMAX[tf], H)
                    if r: rows.append((tf, s, side, x, tg, *r))
                # control: enter at market at the confirmation close (no pullback wait)
                e = now; st = lv(1.0) - buf
                if (e - st) * side > 0 and (lv(tg) - e) * side > 0:
                    r = trade(d, c1 + 1, side, e * (1 + side), st, lv(tg), 1, HOLDMAX[tf], np.inf * side)   # fills at the next open
                    if r: rows.append((tf, s, side, 'market', tg, *r))
T = pd.DataFrame(rows, columns=['tf', 'sym', 'side', 'entry', 'tgt', 't', 'R']); T['half'] = np.where(T.t < SPLIT, 'A', 'B')
T['entry'] = T.entry.astype(str)
print(T.pivot_table(index=['tf', 'tgt'], columns=['entry'], values='R', aggfunc='mean').round(3).to_string())
print('\nentry 0.5, by half and side:')
x = T[T.entry == '0.5']
print(x.pivot_table(index=['tf', 'tgt'], columns=['half', 'side'], values='R', aggfunc='mean').round(3).to_string())
print('\ntrades per tf (entry 0.5, tgt -2):', x[x.tgt == -2].groupby('tf').size().to_dict())
T.to_pickle('fibset/tgt.pkl')
