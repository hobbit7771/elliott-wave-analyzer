import math, numpy as np, pandas as pd, io, contextlib, os, sys
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('combo2.py').read().split("def rep")[0])      # TR (trend R/day), CA (carry return/day)
    os.chdir('..' if os.getcwd().endswith('carry') else '.')
    exec(open('xsf.py').read().split("for lb in")[0])
    XS=run(lb=7,hold=7,q=0.33)                              # funding factor, terciles, weekly
idx=TR.index.intersection(CA.index).intersection(XS.index); idx=idx[idx>='2021-06-01']
T=TR.loc[idx]*0.0025; C=CA.loc[idx]; X=XS.loc[idx]
print('correlations (daily):'); print(pd.DataFrame({'trend':T,'carry':C,'xs_funding':X}).corr().round(2))
def rep(name,r):
    eq=(1+r).cumprod(); yrs=len(r)/365; cagr=eq.iloc[-1]**(1/yrs)-1; v=r.std()*math.sqrt(365); dd=(1-eq/eq.cummax()).max()
    by=r.groupby(r.index.year).apply(lambda x:(1+x).prod()-1)
    h1=r[r.index<'2024-01-01']; h2=r[r.index>='2024-01-01']; s=lambda x:x.mean()/x.std()*math.sqrt(365)
    print(f"{name:52s} CAGR {cagr*100:+6.1f}% vol {v*100:5.1f}% SR {s(r):5.2f} maxDD {dd*100:5.1f}% halves {s(h1):+.2f}/{s(h2):+.2f} | "+' '.join(f"{y}:{x*100:+.0f}%" for y,x in by.items()))
rep('trend (0.25%/trade)',T); rep('carry',C); rep('xs funding (full size, ~18% vol)',X)
rep('trend + carry',T+C)
for k in (0.25,0.5):
    rep(f'trend + carry + xs funding x{k}',T+C+k*X)
# risk parity on trailing 90-day vol (weights from the past only), target 12% portfolio vol
S=pd.DataFrame({'t':T,'x':X})
iv=1/S.rolling(90,min_periods=60).std().shift(1)
w=iv.div(iv.sum(axis=1),axis=0).fillna(0.5)
rp=(w*S).sum(axis=1); scale=(0.12/math.sqrt(365))/rp.rolling(90,min_periods=60).std().shift(1)
rep('risk parity(trend, xs) @12% vol + carry',(rp*scale.clip(upper=3).fillna(1))+C)
