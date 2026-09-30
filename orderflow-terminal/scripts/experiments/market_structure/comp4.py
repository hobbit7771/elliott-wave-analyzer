# Round 20: beta-neutral check. Weekly 10+10, inverse-vol inside each side, then the short side's gross is scaled so the
# portfolio beta to the equal-weight market (60-day daily regression, causal) is zero. Pre-registered combo before the
# holdout: lvf = rank(flow7) - rank(vol30). Also reports the plain (equal-gross) version and each book's realised beta.
import numpy as np, pandas as pd, sys
P = pd.read_pickle('mkt/daily2.pkl'); part = sys.argv[1]
mk = P.groupby('day').r1.mean()
R = P.pivot_table(index='day', columns='sym', values='r1')
m = mk.reindex(R.index)
cov = R.rolling(60, min_periods=40).cov(m); var = m.rolling(60, min_periods=40).var()
BETA = cov.div(var, axis=0)                                      # beta at day D uses data up to D
P = P[P.day < '2024-03-01'] if part == 'dev' else P[P.day >= '2024-03-01']
rk = lambda c: P.groupby('day')[c].rank(pct=True)
P['lowvol'] = -P.vol30; P['lvf'] = rk('flow7') - rk('vol30')
K = 10; COST = 0.00075; HOLD = 7
def run(col, neutral):
    days = sorted(P.day.unique()); w = pd.Series(dtype=float); out = []
    for i, d in enumerate(days[:-1]):
        turn = 0.0
        if i % HOLD == 0:
            g = P[P.day == d].dropna(subset=[col, 'vol30']).set_index('sym')
            if len(g) >= 2 * K:
                g = g.sort_values(col); iv = 1 / g.vol30; L = g.index[-K:]; S = g.index[:K]
                wl = iv[L] / iv[L].sum(); ws = iv[S] / iv[S].sum()
                if neutral:
                    b = BETA.loc[d]; bl = (wl * b.reindex(L).fillna(1)).sum(); bs = (ws * b.reindex(S).fillna(1)).sum()
                    if bl > 0 and bs > 0: ws = ws * min(3, bl / bs)            # short gross so that beta_long = beta_short
                nw = pd.concat([wl, -ws]); turn = nw.sub(w, fill_value=0).abs().sum(); w = nw
        r = np.exp(R.loc[days[i + 1]].reindex(w.index).fillna(0)) - 1
        out.append((days[i + 1], (w * r).sum() - COST * turn)); w = w * (1 + r)
    s = pd.Series(dict(out)); eq = (1 + s).cumprod(); yrs = len(s) / 365
    mm = (np.exp(m.reindex(s.index)) - 1); beta = s.cov(mm) / mm.var()
    return eq.iloc[-1] ** (1 / yrs) - 1, s.mean() / s.std() * np.sqrt(365), (1 - eq / eq.cummax()).max(), beta, s
res = {}
for col in ('flow7', 'lowvol', 'lvf'):
    for neutral in (False, True):
        c, sh, dd, b, s = run(col, neutral); res[(col, neutral)] = s
        print(f"{part.upper()} {col:7s} {'beta-neutral' if neutral else 'equal gross ':12s} CAGR {c * 100:+6.1f}%  Sharpe {sh:5.2f}  maxDD {dd * 100:5.1f}%  beta {b:+.2f}", flush=True)
pd.to_pickle(res, f'mkt/comp4_{part}.pkl')
