# Round 19 discovery: rank IC of the new flow indicators, and incremental IC over flow7 (residual after flow7).
import sys, numpy as np, pandas as pd
P = pd.read_pickle('mkt/daily2.pkl'); part = sys.argv[1]
P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
F = ['flow7', 'flow3', 'flow14', 'flow30', 'flow_accel', 'flow_vw7', 'flow_consist', 'flow_big', 'flow_quiet', 'flow_dip', 'flow_rip',
     'flow_asia', 'flow_eu', 'flow_us', 'imb_norm', 'flow_resid', 'flow_x_imbnorm']
P['rf7'] = P.groupby('day').flow7.rank(pct=True)
wk = P[P.day.dt.dayofweek == 0]                                     # weekly, non-overlapping for y7
def ic(g, f, y):
    ok = g[f].notna() & g[y].notna()
    return g.loc[ok, f].rank().corr(g.loc[ok, y].rank()) if ok.sum() > 15 else np.nan
def partial(g, f):
    ok = g[f].notna() & g.y7.notna() & g.rf7.notna()
    if ok.sum() < 15: return np.nan
    a = g.loc[ok, f].rank(pct=True); b = g.loc[ok, 'rf7']; y = g.loc[ok, 'y7'].rank(pct=True)
    ra = a - np.polyval(np.polyfit(b, a, 1), b); ry = y - np.polyval(np.polyfit(b, y, 1), b)
    return np.corrcoef(ra, ry)[0, 1]
rows = []
for f in F:
    s7 = wk.groupby('day').apply(lambda g: ic(g, f, 'y7')); s1 = P.groupby('day').apply(lambda g: ic(g, f, 'y1'))
    pp = wk.groupby('day').apply(lambda g: partial(g, f)) if f != 'flow7' else pd.Series([np.nan])
    t = lambda s: s.mean() / s.std() * np.sqrt(s.count())
    rows.append((f, s1.mean(), t(s1), s7.mean(), t(s7), pp.mean(), t(pp)))
print(part.upper()); print(pd.DataFrame(rows, columns=['feature', 'IC1', 't1', 'IC7', 't7', 'pIC7|flow7', 't_p']).round(3).to_string(index=False))
# market timing: equal-weight market next 7 days vs market-wide flow
m = P.groupby('day').agg(mf=('mkt_flow', 'first'), r=('r1', 'mean')); m['fwd7'] = m.r[::-1].rolling(7).sum()[::-1].shift(-1)
mw = m[m.index.dayofweek == 0].dropna()
print('market flow timing (weekly): rank corr', round(mw.mf.corr(mw.fwd7, method='spearman'), 3), 'n', len(mw),
      '| next-7d market % by mkt_flow quartile', (mw.groupby(pd.qcut(mw.mf, 4, labels=False)).fwd7.mean() * 100).round(2).tolist())
