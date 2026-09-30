# MQL5 idea (Volume Profile Levels, Multi-Day VWAP): previous-day POC / value area (70 %) as levels vs PDH/PDL.
# Same event and trade rules as round 11: first touch per day, decide at the close of the 5-min bar, stop 1 ATR(1h), target 2 ATR.
import run, numpy as np, pandas as pd
D = run.load()
res = {}
for s in run.SYMS:
    m = D[s][0]; days = sorted(set(m.index.normalize()))
    for day in days[1:]:
        prev = m[(m.index >= day - pd.Timedelta(days=1)) & (m.index < day)]
        today = m[(m.index >= day) & (m.index < day + pd.Timedelta(days=1))]
        if len(prev) < 1000 or len(today) < 1000: continue
        v = (prev.bq + prev.sq).values; px = ((prev.h + prev.l + prev.c) / 3).values
        bins = np.linspace(prev.l.min(), prev.h.max(), 61); idx = np.clip(np.digitize(px, bins) - 1, 0, 59)
        prof = np.bincount(idx, weights=v, minlength=60); poc = prof.argmax(); lo = hi = poc; tot = prof[poc]
        while tot < 0.7 * prof.sum():
            a = prof[lo - 1] if lo > 0 else -1; b = prof[hi + 1] if hi < 59 else -1
            if a >= b: lo -= 1; tot += a
            else: hi += 1; tot += b
        mid = lambda k: (bins[k] + bins[k + 1]) / 2
        levels = {'VAH': bins[hi + 1], 'VAL': bins[lo], 'POC': mid(poc), 'PDH': prev.h.max(), 'PDL': prev.l.min()}
        vwap = (px * v).sum() / v.sum(); levels['VWAP'] = vwap
        first = today.c.iloc[0]
        for name, lv in levels.items():
            above = first > lv   # approach from above -> support test, from below -> resistance test
            hit = today[today.l <= lv] if above else today[today.h >= lv]
            if not len(hit): continue
            t = hit.index[0]; dec = t.floor('5min') + pd.Timedelta(minutes=5)
            if dec >= today.index[-1] - pd.Timedelta(hours=1): continue
            e = {'sym': s, 'level': ('PDL' if above else 'PDH'), 'price': float(lv), 'touch': t, 'dec': dec}
            pic, p0, a1 = run.picture(D, e)
            for kind in ('fade', 'breakout'):
                r = run.simulate(D, e, *run.rule(D, e, kind, p0, a1))
                if r: res.setdefault((name, kind), []).append(r['R'])
print(f"{'level':6s} {'rule':9s} {'n':>4s} {'win%':>5s} {'avgR':>7s}")
for (name, kind), R in sorted(res.items()):
    R = np.array(R); rng = np.random.default_rng(0); bs = [rng.choice(R, len(R)).mean() for _ in range(2000)]
    print(f"{name:6s} {kind:9s} {len(R):4d} {np.mean(R > 0) * 100:5.0f} {R.mean():+7.3f}  CI90 [{np.percentile(bs, 5):+.2f},{np.percentile(bs, 95):+.2f}]")
