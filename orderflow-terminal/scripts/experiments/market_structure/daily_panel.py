# Daily panel (UTC days) from hourly bars for every coin while in the monthly top-50, with ORIGINAL candidate indicators.
# Every feature at day D uses data up to the close of day D only; targets are returns after D.
import sys, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
rows = []
for s in members():
    h = load(s)
    r1h = np.log(h.c).diff(); imb1h = (2 * h.tb - h.v) / h.v.replace(0, np.nan)      # hourly taker imbalance in [-1, 1]
    # absorption / price impact: slope of hourly return on hourly imbalance over rolling 7 days vs 30 days
    cov7 = (r1h * imb1h).rolling(168, min_periods=100).mean() - r1h.rolling(168, min_periods=100).mean() * imb1h.rolling(168, min_periods=100).mean()
    var7 = imb1h.rolling(168, min_periods=100).var()
    lam7 = cov7 / var7
    cov30 = (r1h * imb1h).rolling(720, min_periods=400).mean() - r1h.rolling(720, min_periods=400).mean() * imb1h.rolling(720, min_periods=400).mean()
    lam30 = cov30 / imb1h.rolling(720, min_periods=400).var()
    up = r1h.clip(lower=0) ** 2; dn = r1h.clip(upper=0) ** 2
    g = h.resample('D')
    d = pd.DataFrame({'o': g.o.first(), 'h': g.h.max(), 'l': g.l.min(), 'c': g.c.last(), 'qv': g.qv.sum(), 'v': g.v.sum(), 'tb': g.tb.sum(),
                      'fund': g.fund.mean(), 'inu': g.inu.last(), 'lam7': lam7.resample('D').last(), 'lam30': lam30.resample('D').last(),
                      'semi_up': up.resample('D').sum(), 'semi_dn': dn.resample('D').sum(), 'maxh': r1h.resample('D').max()})
    lc = np.log(d.c); r = lc.diff(); vol = r.rolling(30, min_periods=20).std()
    d['r1'] = r; d['vol30'] = vol
    d['imb'] = (2 * d.tb - d.v) / d.v.replace(0, np.nan)
    # --- original candidate indicators ---
    d['flow7'] = d.imb.rolling(7).mean()                                   # persistent aggressive flow
    d['absorb'] = np.log((d.lam7.abs() + 1e-9) / (d.lam30.abs() + 1e-9))     # impact now vs usual (low = flow absorbed)
    d['effort_result'] = (lc - lc.shift(7)) / vol / (np.log(d.qv.rolling(7).sum() / d.qv.rolling(60).sum().div(60 / 7)).clip(-3, 3) + 3)  # move per volume effort
    d['crowd'] = d.fund.rolling(7).mean() * 1e4 * ((d.c / d.c.rolling(20).mean() - 1) / vol)   # crowding: funding x extension
    d['fund_div'] = (lc - lc.shift(7)) / vol * -np.sign(d.fund.rolling(7).mean() - d.fund.rolling(7).mean().shift(7))  # price up while funding falls
    d['skew14'] = np.log((d.semi_up.rolling(14).sum() + 1e-12) / (d.semi_dn.rolling(14).sum() + 1e-12))   # upside/downside variance
    d['max_ret'] = d.maxh.rolling(14).max() / vol                         # lottery-like spikes
    d['volterm'] = np.log(r.rolling(3).std() / vol)
    d['relvol'] = np.log(d.qv / d.qv.rolling(30).median())
    d['mom7'] = (lc - lc.shift(7)) / vol; d['mom30'] = (lc - lc.shift(30)) / vol
    d['hi20'] = d.c >= d.h.rolling(20).max().shift(1)                     # new 20-day high today
    d['y1'] = lc.shift(-1) - lc; d['y3'] = lc.shift(-3) - lc; d['y7'] = lc.shift(-7) - lc
    d['sym'] = s
    rows.append(d[d.inu.astype(bool)])
P = pd.concat(rows); P.index.name = 'day'; P = P.reset_index()
mkt = P.groupby('day').r1.mean(); P['mret'] = P.day.map(mkt)
P['resid1'] = P.r1 - P.mret                                                # coin vs market today
breadth = P.groupby('day').hi20.mean(); P['breadth'] = P.day.map(breadth); P['breadth_chg'] = P.day.map(breadth - breadth.shift(5))
P.to_pickle(f'{S}/mkt/daily.pkl'); print(P.shape, P.sym.nunique(), P.day.min(), P.day.max())
