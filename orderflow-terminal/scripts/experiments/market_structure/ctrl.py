# Partial IC of the leverage indicators controlling for volatility (vol30), relative volume and flow7 together.
import sys, numpy as np, pandas as pd
P = pd.read_pickle('mkt/daily3.pkl'); part = sys.argv[1]
V = pd.read_pickle('mkt/daily.pkl')[['day', 'sym', 'relvol', 'skew14', 'max_ret']]
P = P.merge(V, on=['day', 'sym'], how='left')
P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
P['oi_turb'] = P.lev_build - P.liq_drop                                    # size of the biggest hourly OI jump + flush
wk = P[P.day.dt.dayofweek == 0]
CTRL = ['vol30', 'relvol', 'flow7', 'max_ret']
def partial(g, f, y):
    cols = [f, y] + CTRL; h = g[cols].dropna()
    if len(h) < 15: return np.nan
    r = h.rank(pct=True); X = np.c_[np.ones(len(r)), r[CTRL].values]
    res = lambda v: v - X @ np.linalg.lstsq(X, v, rcond=None)[0]
    return np.corrcoef(res(r[f].values), res(r[y].values))[0, 1]
t = lambda s: s.mean() / s.std() * np.sqrt(s.count())
print(part.upper(), 'partial IC controlling for', CTRL)
for f in ('vol30', 'liq_drop', 'lev_build', 'oi_turb', 'liq_long', 'oi_chg30', 'crowd_z', 'crowd_chg7', 'flow_newlong', 'top_acc'):
    ctl = [c for c in CTRL if c != f]
    def pp(g, f=f, ctl=ctl):
        global CTRL
        keep = CTRL; CTRL = ctl
        try: return partial(g, f, 'y7')
        finally: CTRL = keep
    s7 = wk.groupby('day').apply(pp); s1 = P.groupby('day').apply(lambda g: pp(g).__class__ and 0) if False else None
    print(f"{f:13s} pIC7 {s7.mean():+.3f} (t {t(s7):+.2f})")
