# Consolidations ("boxes") and their exits on hourly bars of the monthly top-50 coins, 2021-09 .. 2026-08. Causal definitions:
#   box forms at bar t when the 48h high-low range, in units of ATR(1h,14d)*sqrt(48), is below its own 20th percentile of the
#   previous 90 days; bounds = high/low of those 48 bars; it lasts while hourly closes stay inside; exit = first close outside.
# Features known at the exit bar's open (before the breakout close) and outcomes after the exit close.
import sys, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
W = 48
rows = []
btc = load('BTCUSDT'); btc_r7 = np.log(btc.c).diff(168)
for s in members():
    d = load(s); a = atr(d).values; c, h, l, o = d.c.values, d.h.values, d.l.values, d.o.values
    hi = d.h.rolling(W).max().values; lo = d.l.rolling(W).min().values
    width = (hi - lo) / (a * np.sqrt(W)); thr = pd.Series(width).rolling(24 * 90, min_periods=24 * 30).quantile(0.2).shift(1).values
    tbr = (d.tb / d.v.replace(0, np.nan)).values; v = d.v.values; lc = np.log(c); fund = d.fund.values; inu = d.inu.values
    hi20 = d.h.rolling(480).max().values; lo20 = d.l.rolling(480).min().values; vol = (np.diff(lc, prepend=lc[0]))
    rv = pd.Series(vol).rolling(24 * 14, min_periods=24 * 7).std().values
    bt = btc_r7.reindex(d.index).values
    i = W + 24 * 30; L = len(d)
    while i < L - 73:
        if not (inu[i] and np.isfinite(thr[i]) and width[i] <= thr[i]): i += 1; continue
        bh, bl, t0 = hi[i], lo[i], i
        j = i + 1
        while j < L - 73 and bl <= c[j] <= bh: j += 1
        if j >= L - 73: break
        dirn = 1 if c[j] > bh else -1
        seg = slice(t0 - W + 1, j)                      # the box bars (exit bar excluded)
        x = dict(sym=s, t=d.index[j], dur=j - t0 + W, width=(bh - bl) / (a[j] * np.sqrt(W)), dirn=dirn,
                 prior7=(lc[t0 - W] - lc[t0 - W - 168]) / (rv[j] * np.sqrt(168)),
                 pos20=((bh + bl) / 2 - lo20[j - 1]) / max(hi20[j - 1] - lo20[j - 1], 1e-12),
                 closeloc=(c[j - 1] - bl) / max(bh - bl, 1e-12),
                 tb_box=np.nansum(d.tb.values[seg]) / max(np.nansum(v[seg]), 1e-12) - 0.5,
                 tb_last=np.nansum(d.tb.values[j - 6:j]) / max(np.nansum(v[j - 6:j]), 1e-12) - 0.5,
                 volslope=np.log((v[j - 12:j].mean() + 1) / (v[t0 - W + 1:t0 - W + 13].mean() + 1)),
                 fund=fund[j - 1], btc7=bt[j - 1],
                 # the same information but known at the box FORMATION (tradeable before the breakout)
                 f_tb=np.nansum(d.tb.values[t0 - W + 1:t0 + 1]) / max(np.nansum(v[t0 - W + 1:t0 + 1]), 1e-12) - 0.5,
                 f_loc=(c[t0] - bl) / max(bh - bl, 1e-12), f_fund=fund[t0], f_btc7=bt[t0], f_pos20=((bh + bl) / 2 - lo20[t0]) / max(hi20[t0] - lo20[t0], 1e-12),
                 f_prior=(lc[t0] - lc[t0 - 168]) / (rv[t0] * np.sqrt(168)), mid=(bh + bl) / 2,
                 # outcomes in box widths and in the breakout direction
                 r24=(lc[j + 24] - lc[j]) * dirn / (rv[j] * np.sqrt(24)), r72=(lc[j + 72] - lc[j]) * dirn / (rv[j] * np.sqrt(72)),
                 false12=bool(np.any((c[j + 1:j + 13] < bh) if dirn > 0 else (c[j + 1:j + 13] > bl))),
                 other_side=bool(np.any((l[j + 1:j + 73] < bl) if dirn > 0 else (h[j + 1:j + 73] > bh))),
                 move72=(np.max(h[j + 1:j + 73]) - c[j]) / (bh - bl) if dirn > 0 else (c[j] - np.min(l[j + 1:j + 73])) / (bh - bl))
        rows.append(x); i = j + 1
B = pd.DataFrame(rows); B.to_pickle(f'{S}/mkt/boxes.pkl'); print('boxes', len(B), 'coins', B.sym.nunique())
