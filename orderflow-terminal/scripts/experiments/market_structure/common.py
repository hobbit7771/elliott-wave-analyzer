# Shared loaders for the market-structure study: hourly bars of the coins while they were in the monthly top-50.
import json, numpy as np, pandas as pd, os
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
UNI = {pd.Timestamp(k): v for k, v in json.load(open(f'{S}/ml_universe.json')).items()}
def members(): return sorted(set(x for u in UNI.values() for x in u))
def load(sym):
    z = np.load(f'{S}/h1/{sym}.npz'); k = z['k']
    d = pd.DataFrame(k, columns=['t', 'o', 'h', 'l', 'c', 'v', 'qv', 'tb']); d.index = pd.to_datetime(d.t.astype(np.int64), unit='ms')
    d = d[~d.index.duplicated()].asfreq('h'); d['c'] = d.c.ffill()
    for c in ('o', 'h', 'l'): d[c] = d[c].fillna(d.c)
    d[['v', 'qv', 'tb']] = d[['v', 'qv', 'tb']].fillna(0)
    f = z['f']
    d['fund'] = pd.Series(f[:, 1], index=pd.to_datetime(f[:, 0].astype(np.int64), unit='ms')).sort_index().groupby(level=0).last().reindex(d.index, method='ffill') if len(f) else np.nan
    # in-universe mask
    m = np.zeros(len(d), bool)
    for mo, u in UNI.items():
        if sym in u: m |= (d.index >= mo) & (d.index < mo + pd.offsets.MonthBegin(1))
    d['inu'] = m
    return d
def atr(d, n=24 * 14):
    pc = d.c.shift(); tr = np.maximum(d.h - d.l, np.maximum((d.h - pc).abs(), (d.l - pc).abs()))
    return tr.rolling(n, min_periods=n // 2).mean()
def zigzag(d, k):
    """causal swing points: a high is confirmed when price falls k x ATR(1h, 14d) x sqrt(24) (≈ k daily ATRs) below it.
    returns list of (idx_extreme, idx_confirm, price, +1 high / -1 low)"""
    a = (atr(d) * np.sqrt(24)).values; h, l = d.h.values, d.l.values
    piv = []; dirn = 0; ext_i = 0; ext_p = d.c.values[0]
    for i in range(len(d)):
        if not np.isfinite(a[i]): continue
        th = k * a[i]
        if dirn >= 0:   # looking for a high (or undecided)
            if h[i] > ext_p or dirn == 0 and h[i] >= ext_p: ext_p, ext_i = h[i], i
            if dirn == 0 and l[i] < ext_p - th: piv.append((ext_i, i, ext_p, 1)); dirn = -1; ext_p, ext_i = l[i], i; continue
            if dirn == 1 and l[i] < ext_p - th: piv.append((ext_i, i, ext_p, 1)); dirn = -1; ext_p, ext_i = l[i], i; continue
        if dirn == -1:
            if l[i] < ext_p: ext_p, ext_i = l[i], i
            if h[i] > ext_p + th: piv.append((ext_i, i, ext_p, -1)); dirn = 1; ext_p, ext_i = h[i], i
        if dirn == 0: dirn = 1
    return piv
