# MQL5 article 16500 (Kelly + Monte-Carlo): scale of the directional book so that the 95th-percentile Monte-Carlo
# drawdown stays within a budget. Scale 1.0 = site (trend 0.25 %/trade, ½ factor, vol-managed) + carry unchanged.
import math, numpy as np, pandas as pd, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('port3.py').read().split("print('correlations")[0])
book = T + 0.5 * X; v = book.rolling(30, min_periods=20).std().shift(1); ref = book.expanding(180).std().shift(1)
D = (book * (ref / v).clip(upper=1).fillna(1)).values; Cv = C.values; n = len(D); rng = np.random.default_rng(1)
starts = [rng.integers(0, n - 20, n // 20 + 1) for _ in range(2000)]
def stats(L):
    r = L * D + Cv; eq = np.cumprod(1 + r); cagr = eq[-1] ** (365 / n) - 1; dd = (1 - eq / np.maximum.accumulate(eq)).max()
    dds = []
    for st in starts:
        x = np.concatenate([r[s:s + 20] for s in st])[:n]; e = np.cumprod(1 + x); dds.append((1 - e / np.maximum.accumulate(e)).max())
    return cagr, dd, np.percentile(dds, 50), np.percentile(dds, 95)
# full Kelly for the directional book (continuous approx.): f* = mean / variance
mu, var = D.mean(), D.var(); print(f"directional book: full Kelly scale f* = {mu / var:.1f}x (half Kelly {mu / var / 2:.1f}x)")
for L in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
    c, dd, p50, p95 = stats(L)
    print(f"scale {L:.1f}x (trend risk {0.25 * L:.2f}%/trade): CAGR {c * 100:5.1f}%  hist maxDD {dd * 100:4.1f}%  MC maxDD p50 {p50 * 100:4.1f}%  p95 {p95 * 100:4.1f}%")
