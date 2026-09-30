# Discovery on DEV only (2021-09 .. 2024-02): cross-sectional rank IC of each candidate with the next 1 / 3 / 7 days,
# t-stats from non-overlapping samples, and the consistency across DEV's two halves.
import numpy as np, pandas as pd, sys
P = pd.read_pickle('/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt/daily.pkl').replace([np.inf, -np.inf], np.nan)
part = sys.argv[1] if len(sys.argv) > 1 else 'dev'
P = P[(P.day < '2024-03-01')] if part == 'dev' else P[P.day >= '2024-03-01']
F = ['flow7', 'absorb', 'effort_result', 'crowd', 'fund_div', 'skew14', 'max_ret', 'volterm', 'relvol', 'mom7', 'mom30', 'resid1', 'fund']
def ic(f, y, step):
    days = sorted(P.day.unique())[::step]; x = P[P.day.isin(days)]
    s = x.groupby('day').apply(lambda g: g[f].corr(g[y], method='spearman') if g[[f, y]].dropna().shape[0] > 15 else np.nan).dropna()
    return s
rows = []
for f in F:
    out = [f]
    for y, st in (('y1', 1), ('y3', 3), ('y7', 7)):
        s = ic(f, y, st); h = len(s) // 2
        out += [s.mean(), s.mean() / s.std() * np.sqrt(len(s)), np.sign(s.iloc[:h].mean()) == np.sign(s.iloc[h:].mean())]
    rows.append(out)
T = pd.DataFrame(rows, columns=['feature', 'IC1', 't1', 'same1', 'IC3', 't3', 'same3', 'IC7', 't7', 'same7'])
pd.set_option('display.width', 200); print(part.upper()); print(T.round(3).to_string(index=False))
# market timing: breadth (share of coins at a 20-day high) and its 5-day change vs the next 7 days of the equal-weight market
m = P.groupby('day').agg(b=('breadth', 'first'), bc=('breadth_chg', 'first'), m=('mret', 'first')).sort_index()
m['fwd7'] = m.m[::-1].rolling(7).sum()[::-1].shift(-1)
w = m.iloc[::7].dropna()
for c in ('b', 'bc'):
    q = pd.qcut(w[c], 4, labels=False, duplicates='drop'); print(f"market next-7d return (%) by {c} quartile:", (w.groupby(q).fwd7.mean() * 100).round(2).tolist(), '| rank corr %.2f' % w[c].corr(w.fwd7, method='spearman'))
