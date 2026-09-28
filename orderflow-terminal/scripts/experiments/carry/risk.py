import math, numpy as np, pandas as pd, io, contextlib, os
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('port3.py').read().split("print('correlations")[0])   # T (trend @0.25%), C (carry), X (factor), idx
def rep(name,r):
    eq=(1+r).cumprod(); yrs=len(r)/365; cagr=eq.iloc[-1]**(1/yrs)-1; v=r.std()*math.sqrt(365); dd=(1-eq/eq.cummax()).max()
    h1=r[r.index<'2024-01-01']; h2=r[r.index>='2024-01-01']; s=lambda x:x.mean()/x.std()*math.sqrt(365)
    worst=r.rolling(30).sum().min()
    print(f"{name:52s} CAGR {cagr*100:+6.1f}% vol {v*100:5.1f}% SR {s(r):5.2f} maxDD {dd*100:5.1f}% worst30d {worst*100:+5.1f}% halves {s(h1):+.2f}/{s(h2):+.2f} ret/DD {cagr/dd:.2f}")
    return r
base=rep('static: trend + carry + 0.5 factor', T+C+0.5*X)
# risk parity on trailing 90d vol between trend and factor, scaled to the static book's long-run vol (causal)
S=pd.DataFrame({'t':T,'x':X}); iv=1/S.rolling(90,min_periods=60).std().shift(1); w=iv.div(iv.sum(axis=1),axis=0).fillna(0.5)
rp=(w*S).sum(axis=1)
target=(T+0.5*X).expanding(180).std().shift(1)
scale=(target/rp.rolling(90,min_periods=60).std().shift(1)).clip(upper=3).fillna(1)
RP=rep('risk parity (trend,factor) at static vol + carry', rp*scale+C)
# drawdown brake: halve the directional sleeves while the portfolio is > X% below its peak
for lim in (0.05,0.08,0.10):
    r=[]; eq=1; pk=1; k=1.0
    for t in idx:
        rt=k*(T.loc[t]+0.5*X.loc[t])+C.loc[t]; eq*=1+rt; pk=max(pk,eq); dd=1-eq/pk
        k=0.5 if dd>lim else (1.0 if dd<lim/2 else k); r.append(rt)
    rep(f'static + drawdown brake at {lim:.0%} (x0.5)', pd.Series(r,index=idx))
# volatility targeting of the whole directional book (Moreira-Muir), causal
book=T+0.5*X; v=book.rolling(30,min_periods=20).std().shift(1); ref=book.expanding(180).std().shift(1)
rep('static + vol-managed directional book (cap 1x)', book*(ref/v).clip(upper=1).fillna(1)+C)
