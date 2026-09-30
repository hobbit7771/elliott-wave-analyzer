# Time-of-day / day-of-week seasonality of hourly returns (monthly top-50 coins), stability across two halves,
# and the funding-settlement hours (00/08/16 UTC) conditional on the funding sign.
import sys, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
R = []
for s in members():
    d = load(s); r = np.log(d.c).diff()
    x = pd.DataFrame({'r': r, 'fund': d.fund.shift(1), 'inu': d.inu}); x = x[x.inu & x.r.notna()]
    x['sym'] = s; R.append(x)
R = pd.concat(R); R['hour'] = R.index.hour; R['dow'] = R.index.dayofweek
R['half'] = np.where(R.index < pd.Timestamp('2024-03-01'), 1, 2)
R['xs'] = R.r - R.groupby(level=0).r.transform('mean')     # return minus the cross-section mean (coin vs market)
mk = R.groupby(level=0).r.mean().to_frame('m'); mk['hour'] = mk.index.hour; mk['dow'] = mk.index.dayofweek; mk['half'] = np.where(mk.index < pd.Timestamp('2024-03-01'), 1, 2)
def tab(g, key):
    t = g.groupby(['half', key]).m.agg(['mean', 'std', 'count']); t['t'] = t['mean'] / (t['std'] / np.sqrt(t['count']))
    return (t['mean'] * 1e4).unstack(0).round(2), t['t'].unstack(0).round(1)
m, t = tab(mk, 'hour'); print('MARKET (equal-weight top-50) mean hourly return by UTC hour, bp, halves 1/2, and t-stats:')
print(pd.concat([m, t], axis=1, keys=['bp', 't']).to_string())
print('corr of hourly means between halves: %.2f' % m[1].corr(m[2]))
m, t = tab(mk, 'dow'); print('\nby day of week (0 = Monday), bp per hour:'); print(pd.concat([m, t], axis=1, keys=['bp', 't']).to_string())
# funding hour: coin-vs-market return in the hour after settlement (00/08/16) split by funding sign
F = R[R.hour.isin([0, 8, 16]) & R.fund.notna()]
for lab, sel in (('funding > +0.03%', F.fund > 0.0003), ('funding < -0.03%', F.fund < -0.0003), ('|funding| small', F.fund.abs() <= 0.0003)):
    x = F[sel]['xs']; print(f"hour after settlement, {lab}: coin minus market {x.mean() * 1e4:+.2f} bp (t {x.mean() / (x.std() / np.sqrt(len(x))):.1f}, n {len(x)})")
G = R[R.hour.isin([23, 7, 15]) & R.fund.notna()]
for lab, sel in (('funding > +0.03%', G.fund > 0.0003), ('funding < -0.03%', G.fund < -0.0003)):
    x = G[sel]['xs']; print(f"hour BEFORE settlement, {lab}: coin minus market {x.mean() * 1e4:+.2f} bp (t {x.mean() / (x.std() / np.sqrt(len(x))):.1f}, n {len(x)})")
