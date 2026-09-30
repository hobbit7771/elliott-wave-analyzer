# Round 20: positioning and leverage indicators from Binance public metrics (daily summaries of 5-min data).
# Same pre-registered split: DEV < 2024-03, HOLD >= 2024-03. IC to the next 7 days (weekly, non-overlapping) and the
# partial IC over flow7 (does it add anything to the order-flow factor already in forward test).
import sys, glob, os, numpy as np, pandas as pd
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
if not os.path.exists(f'{S}/mkt/daily3.pkl'):
    P = pd.read_pickle(f'{S}/mkt/daily2.pkl')[['day', 'sym', 'c', 'r1', 'vol30', 'flow7', 'mom7', 'y1', 'y7', 'fund']]
    M = []
    for p in glob.glob(f'{S}/met/*.pkl'):
        m = pd.read_pickle(p)
        if m.empty: continue
        m['day'] = pd.to_datetime(m.day); m = m.set_index('day').sort_index().asfreq('D')
        x = pd.DataFrame(index=m.index)
        l = lambda v: np.log(v.where(v > 0))
        x['oi_chg1'] = l(m.oi).diff(); x['oi_chg7'] = l(m.oi).diff(7); x['oi_chg30'] = l(m.oi).diff(30)
        x['top_pos'] = l(m.top_pos); x['top_acc'] = l(m.top_acc); x['crowd'] = l(m.crowd)
        x['smart_vs_crowd'] = x.top_pos - x.crowd                      # top traders' position tilt vs everyone's account tilt
        x['smart_vs_crowd7'] = x.smart_vs_crowd.rolling(7).mean()
        x['whale_tilt'] = x.top_pos - x.top_acc                         # top traders: size-weighted vs head-count tilt
        x['crowd_chg7'] = x.crowd.diff(7); x['top_chg7'] = x.top_pos.diff(7)
        x['crowd_z'] = (x.crowd - x.crowd.rolling(60, min_periods=30).mean()) / x.crowd.rolling(60, min_periods=30).std()
        x['liq_drop'] = m.oi_min1h.rolling(3).min()                     # biggest hourly OI flush in 3 days
        x['lev_build'] = m.oi_max1h.rolling(3).max()
        x['sym'] = os.path.basename(p)[:-4]
        M.append(x.reset_index())
    M = pd.concat(M)
    P = P.merge(M, on=['day', 'sym'], how='left')
    lc = P.groupby('sym').c.transform(lambda s: np.log(s))
    P['px_chg7'] = P.groupby('sym').c.transform(lambda s: np.log(s).diff(7))
    P['oi_px'] = P.oi_chg7 - P.px_chg7                                    # leverage growing faster than price
    P['liq_long'] = P.liq_drop.where(P.groupby('sym').c.transform(lambda s: np.log(s).diff(3)) < 0)   # flush on a falling price
    P['liq_short'] = P.liq_drop.where(P.groupby('sym').c.transform(lambda s: np.log(s).diff(3)) > 0)
    P['flow_newlong'] = P.flow7.where(P.oi_chg7 > 0)                      # aggressive buying that opens positions
    P['flow_cover'] = P.flow7.where(P.oi_chg7 <= 0)                       # aggressive buying that closes shorts
    P = P.replace([np.inf, -np.inf], np.nan); P.to_pickle(f'{S}/mkt/daily3.pkl')
P = pd.read_pickle(f'{S}/mkt/daily3.pkl'); part = sys.argv[1]
P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
print(part.upper(), 'rows with metrics', P.oi_chg7.notna().sum(), 'of', len(P))
F = ['oi_chg1', 'oi_chg7', 'oi_chg30', 'oi_px', 'top_pos', 'top_acc', 'crowd', 'smart_vs_crowd', 'smart_vs_crowd7', 'whale_tilt', 'crowd_chg7', 'top_chg7',
     'crowd_z', 'liq_drop', 'lev_build', 'liq_long', 'liq_short', 'flow_newlong', 'flow_cover']
P['rf7'] = P.groupby('day').flow7.rank(pct=True)
wk = P[P.day.dt.dayofweek == 0]
def ic(g, f, y):
    ok = g[f].notna() & g[y].notna(); return g.loc[ok, f].rank().corr(g.loc[ok, y].rank()) if ok.sum() > 12 else np.nan
def partial(g, f):
    ok = g[f].notna() & g.y7.notna() & g.rf7.notna()
    if ok.sum() < 12: return np.nan
    a = g.loc[ok, f].rank(pct=True); b = g.loc[ok, 'rf7']; y = g.loc[ok, 'y7'].rank(pct=True)
    return np.corrcoef(a - np.polyval(np.polyfit(b, a, 1), b), y - np.polyval(np.polyfit(b, y, 1), b))[0, 1]
t = lambda s: s.mean() / s.std() * np.sqrt(s.count())
rows = []
for f in F:
    s1 = P.groupby('day').apply(lambda g: ic(g, f, 'y1')); s7 = wk.groupby('day').apply(lambda g: ic(g, f, 'y7')); pp = wk.groupby('day').apply(lambda g: partial(g, f))
    rows.append((f, s1.mean(), t(s1), s7.mean(), t(s7), pp.mean(), t(pp)))
print(pd.DataFrame(rows, columns=['feature', 'IC1', 't1', 'IC7', 't7', 'pIC7', 't_p']).round(3).to_string(index=False))
