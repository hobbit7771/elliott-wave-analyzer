# MQL5 code-base ideas on the site trend model (20-day breakout, 3 ATR stop, 10-day low exit, long only, BTC filter):
#  1) partial close at +1R + break-even for the rest (KSQ FVG EA, cm partial closing, BEC manager)
#  2) R-squared "trend quality" filter at entry (Bands R-squared / Adaptive MACD idea)
import inspect, re, warnings; warnings.filterwarnings('ignore')
import dbt, r2
from r2 import *
src = inspect.getsource(dbt.simulate)
# add trail kind ('donchian_be', n, trigR): donchian exit, and the stop goes to entry once price reached +trigR
src = src.replace("elif trail[0]=='donchian':", """elif trail[0]=='donchian_be':
                    w=trail[1]; ts=l[max(0,k-w+1):k+1].min() if d>0 else h[max(0,k-w+1):k+1].max()
                    stop=max(stop,ts) if d>0 else min(stop,ts)
                    best=(h[j:k+1].max()-px)*d/risk if d>0 else (px-l[j:k+1].min())/risk
                    if best>=trail[2]: stop=max(stop,px) if d>0 else min(stop,px)
                elif trail[0]=='donchian':""").replace('def simulate(', 'def simulate2(')
exec(src, dbt.__dict__); simulate2 = dbt.simulate2
def rsq(c, n):
    y = np.log(c[-n:]); x = np.arange(n); b = np.polyfit(x, y, 1); yh = np.polyval(b, x)
    return 1 - ((y - yh) ** 2).sum() / ((y - y.mean()) ** 2).sum(), b[0]
def run2(name, trail=('donchian', 10), r2n=None, r2min=None, partial=None):
    ps = []; T = []
    for s, df in D.items():
        sig = gen(df)
        if r2n:
            c = df.c.values; keep = []
            for x in sig:
                q, sl = rsq(c[:x['i'] + 1], r2n)
                if q >= r2min and sl > 0: keep.append(x)
            sig = keep
        if partial:
            # leg A: target at +partial R (computed from the stop-entry price); leg B: runner with break-even after +partial R
            sa = [dict(x, target=x['entry'] + (x['entry'] - x['stop']) * partial) for x in sig]
            ta, pa = simulate2(df, sa, s, 10000, trail); tb, pb = simulate2(df, sig, s, 10000, ('donchian_be', trail[1], partial))
            p = 0.5 * pa + 0.5 * pb; tr = [dataclass_r(t, 0.5) for t in ta] + [dataclass_r(t, 0.5) for t in tb]
        else:
            tr, p = simulate2(df, sig, s, 10000, trail)
        ps.append(p.rename(s)); T += tr
    P = pd.concat(ps, axis=1, sort=True).fillna(0).sum(axis=1); P = P[P.index >= '2021-06-01']
    m = metrics(P); f = lambda x: x.mean() / x.std() * math.sqrt(365) if x.std() > 0 else 0
    h1 = P[P.index < '2023-06-01']; h2 = P[P.index >= '2023-06-01']
    print(f"{name:46s} trades {len(T):4d} SR {m['sr']:.2f} sumR {P.sum():+5.0f} DD {m['mdd']:4.0f} halves {f(h1):+.2f}/{f(h2):+.2f}", flush=True)
    return P
def dataclass_r(t, w): t.r *= w; return t
run2('BASE (site trend model)')
for pr in (1.0, 1.5, 2.0): run2(f'partial 50% at +{pr}R, rest BE + 10d exit', partial=pr)
run2('break-even at +1R only (no partial)', trail=('donchian_be', 10, 1.0))
for n in (20, 50):
    for q in (0.3, 0.5, 0.7): run2(f'R2 filter: R2({n}d) >= {q}, slope>0', r2n=n, r2min=q)
