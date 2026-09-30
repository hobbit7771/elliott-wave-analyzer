# Join the causal flow7 (last fully closed day before entry) to the site trend trades (5.3y, 15m engine) and compare.
import json, numpy as np, pandas as pd
P = pd.read_pickle('mkt/daily.pkl')[['day', 'sym', 'flow7', 'relvol', 'absorb']]
for f in ('rtf_15m.json', 'rtf_1d.json'):
    T = pd.DataFrame([t for t in json.load(open(f)) if t['model'] == 'TREND_BREAKOUT' and t['status'] in ('win', 'loss', 'be', 'closed', 'stopped', 'tp', 'sl') or (t['model'] == 'TREND_BREAKOUT' and t.get('r') is not None)])
    T['day'] = pd.to_datetime(T.t, unit='ms').dt.normalize() - pd.Timedelta(days=1); T['sym'] = T.sym + 'USDT'
    T = T.merge(P, on=['day', 'sym'], how='left'); T = T.dropna(subset=['flow7', 'r'])
    print(f"\n{f}: trend trades with flow7 {len(T)}  sumR {T.r.sum():+.0f}  meanR {T.r.mean():+.3f}")
    for part, D in (('DEV <2024-03', T[T.day < '2024-03-01']), ('HOLD >=2024-03', T[T.day >= '2024-03-01'])):
        a, b = D[D.flow7 > 0], D[D.flow7 <= 0]
        bs = [np.random.default_rng(i).choice(a.r, len(a)).mean() - np.random.default_rng(i + 999).choice(b.r, len(b)).mean() for i in range(4000)]
        print(f"  {part}: flow7>0 n {len(a):3d} meanR {a.r.mean():+.3f} sumR {a.r.sum():+5.0f} | flow7<=0 n {len(b):3d} meanR {b.r.mean():+.3f} sumR {b.r.sum():+5.0f} | P(diff<=0) {np.mean(np.array(bs) <= 0):.3f}")
