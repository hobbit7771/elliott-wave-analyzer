# Pre-registered composite of the original indicators, weekly long/short and long-only portfolios with taker costs.
# score = rank(flow7) - rank(relvol) - rank(skew14) - rank(absorb)  (cross-sectional ranks within the day)
import numpy as np, pandas as pd, sys
P = pd.read_pickle('/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt/daily.pkl').replace([np.inf, -np.inf], np.nan)
part = sys.argv[1]; P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
K = 10; COST = 0.00075; HOLD = 7
rk = lambda c: P.groupby('day')[c].rank(pct=True)
P['score'] = rk('flow7') - rk('relvol') - rk('skew14') - rk('absorb')
for c in ('flow7', 'relvol', 'skew14', 'absorb'): P['s_' + c] = rk(c) * (1 if c == 'flow7' else -1)
R = P.pivot_table(index='day', columns='sym', values='r1')     # daily log returns (day D's close to D+1 is r1 of D+1)
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
for col in ('score', 's_flow7', 's_relvol', 's_skew14', 's_absorb'):
    for rule in ('ls', 'lo'):
        c, sh, dd, s = run(col, rule); res[(col, rule)] = s
        print(f"{part.upper()} {col:9s} {'long/short' if rule == 'ls' else 'long only ':10s}  CAGR {c * 100:+6.1f}%  Sharpe {sh:5.2f}  maxDD {dd * 100:5.1f}%")
# market (equal-weight) for reference over the same days
m = P.groupby('day').r1.mean().iloc[1:]; m = np.exp(m) - 1; eq = (1 + m).cumprod()
print(f"{part.upper()} market equal-weight            CAGR {(eq.iloc[-1] ** (365 / len(m)) - 1) * 100:+6.1f}%  Sharpe {m.mean() / m.std() * np.sqrt(365):5.2f}  maxDD {(1 - eq / eq.cummax()).max() * 100:5.1f}%")
pd.to_pickle(res, f'/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt/comp_{part}.pkl')
