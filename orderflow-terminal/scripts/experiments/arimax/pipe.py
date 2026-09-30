# User's pipeline: market data -> features -> ARIMAX 5-candle forecast -> ML classifier -> regime filter -> rules -> trade.
# Rules fixed before the run (LONG; SHORT mirrored):
#   ARIMAX 5-candle forecast > +0.5 %, price 0..1 daily ATR above a strong support, CVD(5) > 0, volume(5) > 1.2x norm,
#   OI(5) rising, funding neutral (|rate| <= 0.02 %), ML probability > 0.55, BTC above its 50-day SMA.
# Trade: enter next candle open, exit after 5 candles at the close, 0.075 % per side.
# ARIMAX = ARMA(1,1) on 1-candle log returns (%) + exogenous features of the previous candle, refit monthly on the
# last 2000 candles only (walk-forward); 5-step forecast holds the exog at its last value; 95 % interval from psi weights.
import sys, os, numpy as np, pandas as pd, warnings
from multiprocessing import Pool
warnings.filterwarnings('ignore')
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'INJUSDT', 'SUIUSDT', 'UNIUSDT']
TF = sys.argv[1] if len(sys.argv) > 1 else '1h'
H = 5; COST = 0.15   # % per round trip
EX = ['cvd5', 'oi5', 'fund', 'volx', 'dsup', 'dres']

def load(sym):
    z = np.load(f'{S}/h1/{sym}.npz'); k = z['k']
    d = pd.DataFrame(k[:, 1:], columns=['o', 'h', 'l', 'c', 'v', 'qv', 'tb'], index=pd.to_datetime(k[:, 0].astype(np.int64), unit='ms'))
    d = d[~d.index.duplicated()].sort_index()
    f = z['f']; fs = pd.Series(f[:, 1], index=pd.to_datetime(f[:, 0].astype(np.int64), unit='ms')).sort_index()
    fs = fs[~fs.index.duplicated()]
    oi = pd.read_pickle(f'{S}/arimax/oi/{sym}.pkl'); oi = oi[~oi.index.duplicated()]
    if TF == '4h':
        d = d.resample('4h').agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'v': 'sum', 'qv': 'sum', 'tb': 'sum'}).dropna()
    d['fund'] = fs.reindex(d.index, method='ffill')
    d['oi'] = oi.reindex(d.index, method='ffill') if TF == '1h' else oi.resample('4h').last().reindex(d.index, method='ffill')
    return d[d.index >= '2021-12-01']

def levels(d):
    """strong daily support / resistance known at each candle: confirmed daily fractal lows / highs (2 bars each side)
    within 120 days, touched at least twice (another daily low / high within 0.5 daily ATR)."""
    D = d.resample('D').agg({'h': 'max', 'l': 'min', 'c': 'last'}).dropna()
    pc = D.c.shift(); atr = np.maximum(D.h - D.l, np.maximum((D.h - pc).abs(), (D.l - pc).abs())).rolling(14).mean().values
    hi, lo = D.h.values, D.l.values; n = len(D); sup = [[] for _ in range(n)]; res = [[] for _ in range(n)]
    for i in range(5, n):
        a = atr[i - 1]
        if not np.isfinite(a): continue
        for j in range(max(2, i - 120), i - 2):                     # fractal at j confirmed at j+2 < i
            if lo[j] == lo[j - 2:j + 3].min() and (np.abs(lo[max(0, i - 120):i] - lo[j]) < 0.5 * a).sum() >= 2: sup[i].append(lo[j])
            if hi[j] == hi[j - 2:j + 3].max() and (np.abs(hi[max(0, i - 120):i] - hi[j]) < 0.5 * a).sum() >= 2: res[i].append(hi[j])
    day_idx = np.searchsorted(D.index.values, d.index.floor('D').values)
    c = d.c.values; dsup = np.full(len(d), np.nan); dres = np.full(len(d), np.nan)
    for k in range(len(d)):
        i = day_idx[k]
        if i >= n or not np.isfinite(atr[max(i - 1, 0)]): continue
        a = atr[i - 1]; s = [x for x in sup[i] if x <= c[k]]; r = [x for x in res[i] if x >= c[k]]
        if s: dsup[k] = (c[k] - max(s)) / a
        if r: dres[k] = (min(r) - c[k]) / a
    return dsup, dres

def features(sym):
    d = load(sym)
    d['r'] = np.log(d.c).diff() * 100
    d['cvd5'] = (2 * d.tb - d.v).rolling(H).sum() / d.v.rolling(H).sum()
    d['oi5'] = np.log(d.oi).diff(H) * 100
    per_day = 24 if TF == '1h' else 6
    v5 = d.v.rolling(H).sum(); d['volx'] = np.log(v5 / v5.rolling(30 * per_day, min_periods=10 * per_day).median())
    d['dsup'], d['dres'] = levels(d)
    d['fwd'] = (np.log(d.c.shift(-H)) - np.log(d.o.shift(-1))) * 100    # enter next open, exit after 5 candles
    d['sym'] = sym
    return d

def arimax(d):
    """walk-forward ARIMAX: monthly refit on the last 2000 candles; returns 5-candle forecast (%) and its 95 % half-width"""
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    X = d[EX].copy(); X['dsup'] = X.dsup.clip(0, 5).fillna(5); X['dres'] = X.dres.clip(0, 5).fillna(5); X['fund'] = X.fund * 1e4
    X = X.shift(1)                                                    # exog of the previous candle
    y = d.r.values; Xv = X.values; n = len(d)
    ok = np.isfinite(y) & np.isfinite(Xv).all(1)
    F = np.full(n, np.nan); W = np.full(n, np.nan)
    months = pd.date_range(d.index[0].normalize() + pd.offsets.MonthBegin(1), d.index[-1], freq='MS')
    u_prev = 0.0; e_prev = 0.0
    for m0, m1 in zip(months, list(months[1:]) + [d.index[-1] + pd.Timedelta(hours=1)]):
        tr = np.where(ok & (d.index < m0))[0][-2000:]
        if len(tr) < 1500: continue
        try:
            res = SARIMAX(y[tr], exog=Xv[tr], order=(1, 0, 1), trend='c').fit(disp=False, maxiter=200)
        except Exception: continue
        p = dict(zip(res.model.param_names, res.params))
        c0 = p['intercept']; b = np.array([p[f'x{i + 1}'] for i in range(len(EX))]); phi = p['ar.L1']; th = p['ma.L1']; s2 = p['sigma2']
        psi = [1.0] + [phi ** (j - 1) * (phi + th) for j in range(1, H)]
        half = 1.96 * np.sqrt(s2 * sum(sum(psi[:k + 1]) ** 2 for k in range(H)))
        geo = sum(phi ** j for j in range(H))
        idx = np.where((d.index >= m0) & (d.index < m1))[0]
        for t in idx:
            if not ok[t]: continue
            u = y[t] - c0 - Xv[t] @ b                                  # regression residual (ARMA part)
            e = u - phi * u_prev - th * e_prev
            u_prev, e_prev = u, e
            # next candle's exog = features of candle t (known now), held for the 5 steps
            xn = d[EX].iloc[t].values.astype(float).copy()
            xn[EX.index('dsup')] = np.clip(np.nan_to_num(xn[EX.index('dsup')], nan=5), 0, 5); xn[EX.index('dres')] = np.clip(np.nan_to_num(xn[EX.index('dres')], nan=5), 0, 5); xn[EX.index('fund')] *= 1e4
            if not np.isfinite(xn).all(): continue
            F[t] = H * (c0 + xn @ b) + (phi * u + th * e) * geo
            W[t] = half
    return F, W

def one(sym):
    out = f'{S}/arimax/{TF}_{sym}.pkl'
    if os.path.exists(out): return out
    d = features(sym); d['F'], d['W'] = arimax(d); d.to_pickle(out); print(sym, flush=True); return out

if __name__ == '__main__':
    with Pool(6) as p: p.map(one, COINS)
    print('done', TF, flush=True)
