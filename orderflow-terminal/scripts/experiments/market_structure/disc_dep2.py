# Round 21: order-book depth indicators (Binance bookDepth, ±1/2/5 % notional every ~30 s, daily summaries).
# Split fixed before looking: DEV 2023-01..2024-08, HOLD 2024-09..2026-08. IC to the next 1 and 7 days and the partial IC
# over flow7 and vol30 (the two effects already known). Variant: controls also relative volume and book size.
import sys, glob, os, numpy as np, pandas as pd
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
P = pd.read_pickle(f'{S}/mkt/daily2.pkl')[['day', 'sym', 'c', 'v', 'r1', 'vol30', 'flow7', 'imb', 'mom7', 'y1', 'y7']]
P = P[P.day >= '2023-01-01']
D = []
for p in glob.glob(f'{S}/dep/*.pkl'):
    d = pd.read_pickle(p)
    if d.empty: continue
    d['day'] = pd.to_datetime(d.day); d = d.set_index('day').sort_index().asfreq('D')
    x = pd.DataFrame(index=d.index)
    for k in (1, 2, 5): x[f'imb{k}'] = d[f'imb{k}']; x[f'imb{k}_7'] = d[f'imb{k}'].rolling(7, min_periods=4).mean()
    x['imb1_late'] = d.imb1_late
    x['far_support'] = d.imb5 - d.imb1                                             # bids waiting lower vs near the price
    dep1 = d.bid1 + d.ask1; x['dep1'] = dep1
    x['liq_withdraw'] = np.log(dep1 / dep1.rolling(30, min_periods=15).median())    # depth now vs usual
    x['liq_withdraw7'] = np.log(dep1.rolling(7, min_periods=4).mean() / dep1.rolling(60, min_periods=30).median())
    x['sym'] = os.path.basename(p)[:-4]
    D.append(x.reset_index())
D = pd.concat(D)
P = P.merge(D, on=['day', 'sym'], how='left').replace([np.inf, -np.inf], np.nan)
P['qv'] = P.c * P.v
V = pd.read_pickle(f'{S}/mkt/daily.pkl')[['day', 'sym', 'relvol']]; P = P.merge(V, on=['day', 'sym'], how='left')
P['dep_level'] = np.log(P.dep1)                                                  # plain size of the book
P['thin'] = -np.log(P.dep1 / P.qv)                                                   # book thin relative to trading
P['absorb_book'] = P.flow7.groupby(P.day).rank(pct=True) - P.imb2_7.groupby(P.day).rank(pct=True)   # buyers push, book not bid
P['book_flow_agree'] = P.flow7.groupby(P.day).rank(pct=True) + P.imb2_7.groupby(P.day).rank(pct=True)
part = sys.argv[1]
P = P[P.day < '2024-09-01'] if part == 'dev' else P[P.day >= '2024-09-01']
print(part.upper(), 'rows', len(P), 'with depth', P.imb1.notna().sum(), 'days', P.day.nunique())
wk = P[P.day.dt.dayofweek == 0]
CTRL = ['flow7', 'vol30', 'relvol', 'dep_level']
def partial(h, f):
    h = h[[f, 'y7'] + CTRL].dropna()
    if len(h) < 15: return np.nan
    r = h.rank(pct=True); X = np.c_[np.ones(len(r)), r[CTRL].values]; res = lambda v: v - X @ np.linalg.lstsq(X, v, rcond=None)[0]
    return np.corrcoef(res(r[f].values), res(r['y7'].values))[0, 1]
t = lambda s: s.mean() / s.std() * np.sqrt(s.count())
rows = []
for f in ('thin', 'imb2_7', 'absorb_book'):
    s1 = P.groupby('day').apply(lambda h: h[f].corr(h.y1, method='spearman')); s7 = wk.groupby('day').apply(lambda h: h[f].corr(h.y7, method='spearman'))
    pp = wk.groupby('day').apply(lambda h: partial(h, f))
    rows.append((f, s1.mean(), t(s1), s7.mean(), t(s7), pp.mean(), t(pp)))
print(pd.DataFrame(rows, columns=['feature', 'IC1', 't1', 'IC7', 't7', 'pIC7', 't_p']).round(3).to_string(index=False))
