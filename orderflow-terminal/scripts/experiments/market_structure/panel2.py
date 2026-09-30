# Round 19: new order-flow indicators derived from hourly taker flow (all causal: day D uses data up to the close of D).
import sys, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
def wflow(tb, v, mask, n=7):
    """volume-weighted taker imbalance over the hours in mask, rolling n days (daily resolution)"""
    b = (2 * tb - v).where(mask, 0).resample('D').sum(); vv = v.where(mask, 0).resample('D').sum()
    return b.rolling(n).sum() / vv.rolling(n).sum().replace(0, np.nan)
rows = []
for s in members():
    h = load(s)
    r1h = np.log(h.c).diff(); hr = h.index.hour
    medv = h.v.rolling(168, min_periods=100).median().shift(1)
    g = h.resample('D')
    d = pd.DataFrame({'c': g.c.last(), 'v': g.v.sum(), 'tb': g.tb.sum(), 'inu': g.inu.last(), 'fund': g.fund.mean()})
    lc = np.log(d.c); r = lc.diff(); vol = r.rolling(30, min_periods=20).std()
    d['r1'] = r; d['vol30'] = vol
    d['imb'] = (2 * d.tb - d.v) / d.v.replace(0, np.nan)
    d['flow7'] = d.imb.rolling(7).mean()
    d['flow3'] = d.imb.rolling(3).mean(); d['flow14'] = d.imb.rolling(14).mean(); d['flow30'] = d.imb.rolling(30).mean()
    d['flow_accel'] = d.flow3 - d.flow30
    d['flow_vw7'] = wflow(h.tb, h.v, pd.Series(True, index=h.index))               # volume-weighted instead of day-mean
    d['flow_consist'] = (d.imb > d.imb.rolling(60, min_periods=30).median()).rolling(7).mean()  # days of above-usual buying
    d['flow_big'] = wflow(h.tb, h.v, h.v > 2 * medv)                                # flow in unusually heavy hours
    d['flow_quiet'] = wflow(h.tb, h.v, h.v <= medv)                                 # flow in quiet hours
    d['flow_dip'] = wflow(h.tb, h.v, r1h < 0)                                       # aggressive buying while price falls
    d['flow_rip'] = wflow(h.tb, h.v, r1h > 0)                                       # aggressive buying while price rises
    d['flow_asia'] = wflow(h.tb, h.v, pd.Series((hr >= 0) & (hr < 8), index=h.index))
    d['flow_eu'] = wflow(h.tb, h.v, pd.Series((hr >= 8) & (hr < 14), index=h.index))
    d['flow_us'] = wflow(h.tb, h.v, pd.Series((hr >= 14), index=h.index))
    d['imb_norm'] = (d.flow7 - d.imb.rolling(90, min_periods=45).mean()) / d.imb.rolling(90, min_periods=45).std()  # vs its own history
    d['mom7'] = (lc - lc.shift(7)) / vol
    d['y1'] = lc.shift(-1) - lc; d['y7'] = lc.shift(-7) - lc
    d['sym'] = s
    rows.append(d[d.inu.astype(bool)])
P = pd.concat(rows); P.index.name = 'day'; P = P.reset_index().replace([np.inf, -np.inf], np.nan)
# flow not explained by the price move: cross-sectional residual of rank(flow7) on rank(mom7)
rk = lambda c: P.groupby('day')[c].rank(pct=True)
P['rf'] = rk('flow7'); P['rm'] = rk('mom7')
def res(g):
    ok = g.rf.notna() & g.rm.notna()
    out = pd.Series(np.nan, index=g.index)
    if ok.sum() > 10: b = np.polyfit(g.rm[ok], g.rf[ok], 1); out[ok] = g.rf[ok] - np.polyval(b, g.rm[ok])
    return out
P['flow_resid'] = P.groupby('day', group_keys=False).apply(res)
P['flow_x_imbnorm'] = P.rf + rk('imb_norm')
P['mkt_flow'] = P.day.map(P.groupby('day').flow7.mean())
P.to_pickle(f'{S}/mkt/daily2.pkl'); print(P.shape, P.sym.nunique(), P.day.min(), P.day.max())
