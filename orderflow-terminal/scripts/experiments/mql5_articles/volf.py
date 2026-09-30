# MQL5 articles 22258/22438/22548 (GARCH family, HAR, rough volatility): better volatility forecast for the risk manager?
# Current site rule: k = min(1, long-run std / last-30-day std) on the directional book (trend + ½ factor); carry as is.
import math, numpy as np, pandas as pd, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('port3.py').read().split("print('correlations")[0])
book = T + 0.5 * X
ref = book.expanding(180).std().shift(1)
def rep(name, k):
    r = book * k.clip(upper=1).fillna(1) + C
    eq = (1 + r).cumprod(); yrs = len(r) / 365; cagr = eq.iloc[-1] ** (1 / yrs) - 1; dd = (1 - eq / eq.cummax()).max()
    s = lambda x: x.mean() / x.std() * math.sqrt(365); h1 = r[r.index < '2024-01-01']; h2 = r[r.index >= '2024-01-01']
    print(f"{name:34s} CAGR {cagr * 100:5.1f}%  SR {s(r):.2f}  maxDD {dd * 100:4.1f}%  worst30d {r.rolling(30).sum().min() * 100:+5.1f}%  halves {s(h1):.2f}/{s(h2):.2f}")
print('static (no scaling)'.ljust(34), end=' '); rep('', pd.Series(1.0, index=book.index))
rep('site: 30-day std', ref / book.rolling(30, min_periods=20).std().shift(1))
for w in (10, 20, 60): rep(f'{w}-day std', ref / book.rolling(w, min_periods=int(w * 0.7)).std().shift(1))
for lam in (0.94, 0.97):
    ew = np.sqrt((book ** 2).ewm(alpha=1 - lam, min_periods=20).mean()).shift(1); rep(f'EWMA lambda {lam}', ref / ew)
# HAR-RV (Corsi 2009): forecast of next-day variance from 1/7/30-day realized variances, refit on an expanding window monthly
rv = book ** 2; f1 = rv; f7 = rv.rolling(7).mean(); f30 = rv.rolling(30).mean()
Xh = pd.concat([f1, f7, f30], axis=1).shift(1); y = rv; pred = pd.Series(np.nan, index=book.index)
for t in range(365, len(book), 30):
    tr = slice(30, t); A = Xh.iloc[tr].dropna(); yy = y.loc[A.index]
    b = np.linalg.lstsq(np.c_[np.ones(len(A)), A.values], yy.values, rcond=None)[0]
    seg = Xh.iloc[t:t + 30]; pred.iloc[t:t + 30] = np.c_[np.ones(len(seg)), seg.values] @ b
har = np.sqrt(pred.clip(lower=1e-10)).rolling(5).mean(); rep('HAR-RV (monthly refit)', ref / har)
# GARCH(1,1) with fixed typical crypto params estimated on the first 365 days only (causal)
from scipy.optimize import minimize
x0 = book.values[:365] - book.values[:365].mean()
def nll(p):
    w, a, b = p
    if w <= 0 or a < 0 or b < 0 or a + b >= 0.999: return 1e10
    h = np.var(x0); ll = 0
    for e in x0: ll += 0.5 * (math.log(h) + e * e / h); h = w + a * e * e + b * h
    return ll
w, a, b = minimize(nll, [np.var(x0) * 0.05, 0.08, 0.9], method='Nelder-Mead').x
h = np.var(x0); hv = []
for e in book.values: hv.append(h); h = w + a * e * e + b * h
g = pd.Series(np.sqrt(hv), index=book.index); rep(f'GARCH(1,1) a={a:.2f} b={b:.2f}', ref / g)
