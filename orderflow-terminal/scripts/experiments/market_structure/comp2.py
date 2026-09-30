# Round 19 portfolios, rules fixed before the holdout (chosen on DEV only):
#   qa  = rank(flow_quiet) - rank(flow_big)                         "quiet minus loud buying"
#   qa2 = rank(flow_quiet) + rank(flow_resid) + rank(flow30) - rank(flow_big)   "quiet accumulation"
# plus flow_quiet, flow_resid alone and flow7 (round 18) as the benchmark. Weekly, 10+10, inverse vol, 0.075 %/side.
import numpy as np, pandas as pd, sys
P = pd.read_pickle('mkt/daily2.pkl'); part = sys.argv[1]
P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
K = 10; COST = 0.00075; HOLD = 7
rk = lambda c: P.groupby('day')[c].rank(pct=True)
P['qa'] = rk('flow_quiet') - rk('flow_big')
P['qa2'] = rk('flow_quiet') + rk('flow_resid') + rk('flow30') - rk('flow_big')
R = P.pivot_table(index='day', columns='sym', values='r1')
def run(col, rule):
    days = sorted(P.day.unique()); w = pd.Series(dtype=float); out = []
    for i, d in enumerate(days[:-1]):
        turn = 0.0
        if i % HOLD == 0:
            g = P[P.day == d].dropna(subset=[col, 'vol30']).set_index('sym')
            if len(g) >= 2 * K:
                g = g.sort_values(col); iv = 1 / g.vol30
                L = g.index[-K:]; S = g.index[:K]; nw = (iv[L] / iv[L].sum())
                if rule == 'ls': nw = pd.concat([nw, -(iv[S] / iv[S].sum())])
                turn = nw.sub(w, fill_value=0).abs().sum(); w = nw
        r = np.exp(R.loc[days[i + 1]].reindex(w.index).fillna(0)) - 1
        out.append((days[i + 1], (w * r).sum() - COST * turn)); w = w * (1 + r)
    s = pd.Series(dict(out)); eq = (1 + s).cumprod(); yrs = len(s) / 365
    return eq.iloc[-1] ** (1 / yrs) - 1, s.mean() / s.std() * np.sqrt(365), (1 - eq / eq.cummax()).max(), s
res = {}
for col in ('flow7', 'flow_quiet', 'flow_resid', 'qa', 'qa2'):
    c, sh, dd, s = run(col, 'ls'); res[col] = s
    print(f"{part.upper()} {col:11s} long/short  CAGR {c * 100:+6.1f}%  Sharpe {sh:5.2f}  maxDD {dd * 100:5.1f}%", flush=True)
pd.to_pickle(res, f'mkt/comp2_{part}.pkl')
