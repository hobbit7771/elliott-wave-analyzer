# Does an original order-flow indicator at the moment of a Donchian-20 daily breakout separate good trend trades from bad?
# Simplified daily trend trade: enter at close of the breakout day, initial stop 3*ATR14, exit on close below prior 10-day low.
# Pre-registered filter: flow7 > 0 (net aggressive buying over the last 7 days). relvol/absorb terciles reported for info.
import numpy as np, pandas as pd, sys
P = pd.read_pickle('mkt/daily.pkl').replace([np.inf, -np.inf], np.nan).sort_values(['sym', 'day'])
COST = 0.00075 * 2
T = []
for s, g in P.groupby('sym'):
    g = g.reset_index(drop=True)
    if len(g) < 60: continue
    tr = np.maximum(g.h - g.l, np.maximum((g.h - g.c.shift()).abs(), (g.l - g.c.shift()).abs()))
    atr = tr.rolling(14).mean().values; hi = g.h.rolling(20).max().shift().values; lo10 = g.l.rolling(10).min().shift().values
    c, l = g.c.values, g.l.values; i = 20; n = len(g)
    while i < n - 1:
        if g.inu[i] and c[i] > hi[i] and np.isfinite(atr[i]):
            e = c[i]; stop = e - 3 * atr[i]; j = i + 1; x = None
            while j < n:
                if l[j] <= stop: x = stop; break
                if c[j] < lo10[j]: x = c[j]; break
                j += 1
            if x is None: x = c[n - 1]; j = n - 1
            T.append(dict(day=g.day[i], sym=s, R=(x - e) / (3 * atr[i]) - COST * e / (3 * atr[i]), ret=x / e - 1 - COST,
                          flow7=g.flow7[i], relvol=g.relvol[i], absorb=g.absorb[i], skew14=g.skew14[i]))
            i = j + 1
        else: i += 1
T = pd.DataFrame(T)
for part, D in (('DEV', T[T.day < '2024-03-01']), ('HOLD', T[T.day >= '2024-03-01'])):
    print(f"\n{part}: trades {len(D)}  all: sumR {D.R.sum():+.0f}  meanR {D.R.mean():+.3f}  win {(D.R > 0).mean():.2f}")
    for name, m in (('flow7>0', D.flow7 > 0), ('flow7<=0', D.flow7 <= 0)):
        x = D[m]; print(f"  {name:10s} n {len(x):4d}  sumR {x.R.sum():+6.0f}  meanR {x.R.mean():+.3f}  win {(x.R > 0).mean():.2f}")
    for f in ('flow7', 'relvol', 'absorb', 'skew14'):
        q = pd.qcut(D[f], 3, labels=False, duplicates='drop')
        print(f"  {f:7s} tercile meanR:", [round(D.R[q == k].mean(), 3) for k in range(3)])
