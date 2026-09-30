# Large ML attempt ("AI trader"): LightGBM on hourly features of the monthly top-50 Binance perps (delisted included),
# monthly walk-forward retraining with a 1-day embargo, daily rebalanced portfolios with taker costs.
# Pre-registered: DEV = 2022-07 .. 2025-09 (may be used to choose between the two portfolio rules below),
# FINAL = 2025-10 .. 2026-09, run once. Success = FINAL beats the site's trend portfolio (return at <= drawdown).
import os, glob, sys, json, numpy as np, pandas as pd, lightgbm as lgb
H = 24; TOP = 50; K = 5; COST = 0.00075        # horizon (h), universe size, names per side, taker fee + slippage per side
DEV0, FIN0, END = pd.Timestamp('2022-07-01'), pd.Timestamp('2025-10-01'), pd.Timestamp('2026-09-25')
F = ['r1', 'r4', 'r12', 'r24', 'r72', 'r168', 'volr', 'vz', 'tb1', 'tb4', 'tb24', 'ch20', 'fund', 'fund7', 'b1', 'b4', 'b24', 'bvol',
     'rk_r24', 'rk_r168', 'rk_fund', 'rk_tb24', 'rk_vz', 'hour', 'dow']

def sym_frame(path):
    z = np.load(path); k = z['k']
    if len(k) < 24 * 90: return None
    d = pd.DataFrame(k, columns=['t', 'o', 'h', 'l', 'c', 'v', 'qv', 'tb']); d.index = pd.to_datetime(d.t.astype(np.int64), unit='ms')
    d = d[~d.index.duplicated()].asfreq('h')
    d['c'] = d.c.ffill(); d[['qv', 'v', 'tb']] = d[['qv', 'v', 'tb']].fillna(0)
    lc = np.log(d.c); r = lc.diff()
    vol = r.rolling(168, min_periods=100).std()
    out = pd.DataFrame(index=d.index)
    for n in (1, 4, 12, 24, 72, 168): out[f'r{n}'] = (lc - lc.shift(n)) / (vol * np.sqrt(n))
    out['volr'] = np.log(r.rolling(24).std() / vol)
    q24 = d.qv.rolling(24).sum(); out['vz'] = np.log((q24 + 1) / (q24.rolling(720, min_periods=240).median() + 1))
    for n in (1, 4, 24): out[f'tb{n}'] = d.tb.rolling(n).sum() / d.v.rolling(n).sum().replace(0, np.nan) - 0.5
    hi = d.h.rolling(480).max(); lo = d.l.rolling(480).min(); out['ch20'] = (d.c - lo) / (hi - lo) - 0.5
    f = z['f']
    if len(f):
        fs = pd.Series(f[:, 1], index=pd.to_datetime(f[:, 0].astype(np.int64), unit='ms')); fs = fs[~fs.index.duplicated()].sort_index()
        fh = fs.reindex(out.index, method='ffill'); out['fund'] = fh
        out['fund7'] = fs.rolling('7D').mean().reindex(out.index, method='ffill')
    else: out['fund'] = np.nan; out['fund7'] = np.nan
    out['qv30'] = d.qv.rolling(720, min_periods=240).sum()           # for the causal universe
    out['vol'] = vol
    out['y'] = (lc.shift(-H) - lc)                                       # forward 24h log return (target, never a feature)
    out['c'] = d.c
    return out

def build():
    if os.path.exists('ml_panel.pkl'): return pd.read_pickle('ml_panel.pkl')
    # pass 1: 30-day quote volume at each month start (causal universe), light
    months = pd.date_range('2021-09-01', END, freq='MS'); qv = {}
    for p in sorted(glob.glob('h1/*.npz')):
        s = os.path.basename(p)[:-4]; k = np.load(p)['k']
        if len(k) < 24 * 90: continue
        t = pd.to_datetime(k[:, 0].astype(np.int64), unit='ms'); q = pd.Series(k[:, 6], index=t)
        q = q[~q.index.duplicated()].asfreq('h').fillna(0).rolling(720, min_periods=240).sum()
        qv[s] = (t[0], t[-1], q)
    uni = {}
    for m in months:
        prev = m - pd.Timedelta(hours=1); vols = {}
        for s, (t0, t1, q) in qv.items():
            if t0 <= m - pd.Timedelta(days=60) and t1 >= m:
                v = q.asof(prev)
                if np.isfinite(v): vols[s] = v
        uni[m] = sorted(vols, key=vols.get, reverse=True)[:TOP]
    del qv
    members = sorted(set(x for u in uni.values() for x in u)); print('symbols ever in the universe:', len(members), flush=True)
    json.dump({str(m.date()): u for m, u in uni.items()}, open('ml_universe.json', 'w'))
    btc = sym_frame('h1/BTCUSDT.npz')
    rows = []
    for s in members:
        f = sym_frame(f'h1/{s}.npz')
        keep = np.zeros(len(f), bool)
        for m, u in uni.items():
            if s in u: keep |= (f.index >= m) & (f.index < m + pd.offsets.MonthBegin(1))
        g = f[keep].copy()
        if g.empty: continue
        for n, col in ((1, 'b1'), (4, 'b4'), (24, 'b24')): g[col] = btc[f'r{n}'].reindex(g.index)
        g['bvol'] = btc['volr'].reindex(g.index); g['sym'] = s
        rows.append(g.astype({c: 'float32' for c in g.columns if c != 'sym'}))
    P = pd.concat(rows); P['hour'] = P.index.hour; P['dow'] = P.index.dayofweek
    for c in ('r24', 'r168', 'fund', 'tb24', 'vz'): P['rk_' + c] = P.groupby(level=0)[c].rank(pct=True)
    P.index.name = 't'; P = P.reset_index()
    P.to_pickle('ml_panel.pkl'); return P

def walk_forward(P):
    P = P[P.t < END]
    P = P.replace([np.inf, -np.inf], np.nan)
    months = pd.date_range(DEV0, END, freq='MS'); preds = []
    params = dict(objective='regression', learning_rate=0.03, num_leaves=31, min_data_in_leaf=500, feature_fraction=0.8,
                  bagging_fraction=0.7, bagging_freq=1, lambda_l2=10, verbose=-1, num_threads=4, seed=1)
    for m in months:
        tr = P[(P.t < m - pd.Timedelta(hours=H + 24)) & (P.t.dt.hour % 4 == 0)].dropna(subset=['y'])
        te = P[(P.t >= m) & (P.t < m + pd.offsets.MonthBegin(1)) & (P.t.dt.hour == 0)]
        if te.empty: continue
        yv = (tr.y / (tr.vol * np.sqrt(H))).clip(-5, 5)                   # vol-normalised target
        mdl = lgb.train(params, lgb.Dataset(tr[F], yv), num_boost_round=400)
        te = te.assign(pred=mdl.predict(te[F])); preds.append(te[['t', 'sym', 'pred', 'y', 'vol']])
        print(m.date(), 'train', len(tr), 'test', len(te), flush=True)
    return pd.concat(preds)

def portfolio(D, rule):
    """daily rebalance at 00:00 UTC; inverse-vol weights, gross exposure 1 per side; taker cost on turnover"""
    out = []; prev = {}
    for t, g in D.groupby('t'):
        g = g.dropna(subset=['pred', 'y'])
        if len(g) < 2 * K: continue
        g = g.sort_values('pred'); w = {}
        longs = g.tail(K); shorts = g.head(K)
        if rule == 'long_only': longs = longs[longs.pred > 0]
        iv = 1 / g.set_index('sym').vol
        if len(longs): wl = iv[longs.sym]; wl = wl / wl.sum(); w.update(wl.to_dict())
        if rule == 'long_short':
            ws = iv[shorts.sym]; ws = ws / ws.sum(); w.update((-ws).to_dict())
        ret = sum(w[s] * (np.exp(y) - 1) for s, y in zip(g.sym, g.y) if s in w)
        turn = sum(abs(w.get(s, 0) - prev.get(s, 0)) for s in set(w) | set(prev))
        out.append((t, ret - COST * turn)); prev = w
    return pd.Series(dict(out))

def stats(r, name):
    if r.empty: return print(name, 'no data')
    eq = (1 + r).cumprod(); yrs = len(r) / 365; cagr = eq.iloc[-1] ** (1 / yrs) - 1; dd = (1 - eq / eq.cummax()).max()
    print(f"{name:40s} days {len(r):4d}  total {(eq.iloc[-1] - 1) * 100:+7.1f}%  CAGR {cagr * 100:+6.1f}%  Sharpe {r.mean() / r.std() * np.sqrt(365):5.2f}  maxDD {dd * 100:5.1f}%")

if __name__ == '__main__':
    P = build(); print('panel', P.shape, P.sym.nunique(), 'symbols', flush=True)
    if os.path.exists('ml_preds.pkl'): D = pd.read_pickle('ml_preds.pkl')
    else: D = walk_forward(P); D.to_pickle('ml_preds.pkl')
    dev = D[D.t < FIN0]; ic = dev.groupby('t').apply(lambda g: g.pred.corr(g.y, method='spearman'))
    print(f"DEV rank IC mean {ic.mean():.4f}, t-stat {ic.mean() / ic.std() * np.sqrt(len(ic)):.2f}")
    for rule in ('long_short', 'long_only'): stats(portfolio(dev, rule), f'DEV {rule}')
    if 'final' in sys.argv:
        fin = D[D.t >= FIN0]; ic = fin.groupby('t').apply(lambda g: g.pred.corr(g.y, method='spearman'))
        print(f"FINAL rank IC mean {ic.mean():.4f}, t-stat {ic.mean() / ic.std() * np.sqrt(len(ic)):.2f}")
        for rule in ('long_short', 'long_only'): stats(portfolio(fin, rule), f'FINAL {rule}')
