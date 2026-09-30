# Round 20b: order flow vs funding / OI context. Partial IC over flow7 AND vol30 (both known effects), weekly y7.
import sys, numpy as np, pandas as pd
P = pd.read_pickle('mkt/daily3.pkl'); part = sys.argv[1]
P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
P['fund7'] = P.groupby('sym').fund.transform(lambda s: s.rolling(7).mean())
g = lambda c: P.groupby('day')[c].rank(pct=True) - 0.5
P['squeeze'] = g('flow7') - g('fund7')                        # buyers aggressive while longs are cheap / shorts paying
P['squeeze_x'] = g('flow7') * -g('fund7')                     # interaction: both extremes together
P['trap'] = -g('flow7') * g('oi_chg7')                        # aggressive selling into rising OI = shorts piling in
P['absorb_oi'] = g('flow7') * -g('px_chg7')                   # buying while price falls
P['fund_mom'] = -g('fund7') * g('mom7')                       # price up while funding low (disbelief rally)
wk = P[P.day.dt.dayofweek == 0]
CTRL = ['flow7', 'vol30']
def partial(h, f):
    h = h[[f, 'y7'] + CTRL].dropna()
    if len(h) < 15: return np.nan
    r = h.rank(pct=True); X = np.c_[np.ones(len(r)), r[CTRL].values]; res = lambda v: v - X @ np.linalg.lstsq(X, v, rcond=None)[0]
    return np.corrcoef(res(r[f].values), res(r['y7'].values))[0, 1]
t = lambda s: s.mean() / s.std() * np.sqrt(s.count())
for f in ('fund7', 'squeeze', 'squeeze_x', 'trap', 'absorb_oi', 'fund_mom'):
    ic = wk.groupby('day').apply(lambda h: h[f].corr(h.y7, method='spearman')); pp = wk.groupby('day').apply(lambda h: partial(h, f))
    print(f"{part.upper()} {f:10s} IC7 {ic.mean():+.3f} (t {t(ic):+.2f})  partial|flow7,vol {pp.mean():+.3f} (t {t(pp):+.2f})")
