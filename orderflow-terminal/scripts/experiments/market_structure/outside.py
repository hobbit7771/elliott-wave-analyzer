# Round 19, third sample (pre-registered before running): coin-days OUTSIDE the monthly top-50 (never used for selection),
# with 30-day median daily quote volume >= $3M. Pre-registered: FQ = rank(flow_rip) - rank(flow_dip) ("flow quality":
# buying that lifts price vs buying absorbed while price falls); also flow_big and partial ICs over flow7.
import sys, glob, os, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
def wflow(tb, v, mask, n=7):
    b = (2 * tb - v).where(mask, 0).resample('D').sum(); vv = v.where(mask, 0).resample('D').sum()
    return b.rolling(n).sum() / vv.rolling(n).sum().replace(0, np.nan)
if not os.path.exists('mkt/outside.pkl'):
    rows = []
    for p in sorted(glob.glob(f'{S}/h1/*.npz')):
        s = os.path.basename(p)[:-4]
        try: h = load(s)
        except Exception: continue
        if len(h) < 24 * 60: continue
        r1h = np.log(h.c).diff(); medv = h.v.rolling(168, min_periods=100).median().shift(1)
        g = h.resample('D')
        d = pd.DataFrame({'c': g.c.last(), 'v': g.v.sum(), 'tb': g.tb.sum(), 'qv': g.qv.sum(), 'inu': g.inu.max()})
        lc = np.log(d.c); r = lc.diff(); d['r1'] = r; d['vol30'] = r.rolling(30, min_periods=20).std()
        d['imb'] = (2 * d.tb - d.v) / d.v.replace(0, np.nan); d['flow7'] = d.imb.rolling(7).mean()
        d['flow_rip'] = wflow(h.tb, h.v, r1h > 0); d['flow_dip'] = wflow(h.tb, h.v, r1h < 0); d['flow_big'] = wflow(h.tb, h.v, h.v > 2 * medv)
        d['liq'] = d.qv.rolling(30, min_periods=20).median().shift(1)
        d['y7'] = lc.shift(-7) - lc; d['sym'] = s
        rows.append(d[(~d.inu.astype(bool)) & (d.liq >= 3e6) & (d.v > 0)])
    P = pd.concat(rows); P.index.name = 'day'; P = P.reset_index().replace([np.inf, -np.inf], np.nan); P.to_pickle('mkt/outside.pkl')
P = pd.read_pickle('mkt/outside.pkl'); P = P[P.day >= '2021-09-01']
print('outside sample', P.shape, P.sym.nunique(), 'coins; per-day median', P.groupby('day').size().median())
rk = lambda c: P.groupby('day')[c].rank(pct=True)
P['FQ'] = rk('flow_rip') - rk('flow_dip'); P['rf7'] = rk('flow7')
wk = P[P.day.dt.dayofweek == 0]
def ic(g, f):
    ok = g[f].notna() & g.y7.notna(); return g.loc[ok, f].rank().corr(g.loc[ok, 'y7'].rank()) if ok.sum() > 15 else np.nan
def partial(g, f):
    ok = g[f].notna() & g.y7.notna() & g.rf7.notna()
    if ok.sum() < 15: return np.nan
    a = g.loc[ok, f].rank(pct=True); b = g.loc[ok, 'rf7']; y = g.loc[ok, 'y7'].rank(pct=True)
    return np.corrcoef(a - np.polyval(np.polyfit(b, a, 1), b), y - np.polyval(np.polyfit(b, y, 1), b))[0, 1]
t = lambda s: s.mean() / s.std() * np.sqrt(s.count())
for f in ('flow7', 'FQ', 'flow_rip', 'flow_dip', 'flow_big'):
    s7 = wk.groupby('day').apply(lambda g: ic(g, f)); pp = wk.groupby('day').apply(lambda g: partial(g, f)) if f != 'flow7' else pd.Series([np.nan])
    h1 = s7[s7.index < '2024-03-01']; h2 = s7[s7.index >= '2024-03-01']
    print(f"{f:9s} IC7 {s7.mean():+.3f} (t {t(s7):+.2f}) | 1st half {h1.mean():+.3f} 2nd half {h2.mean():+.3f} | partial|flow7 {pp.mean():+.3f} (t {t(pp):+.2f})")
