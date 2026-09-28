import math, numpy as np, pandas as pd, os, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('xsf.py').read().split("for lb in")[0])
DV=(P*0+1)  # placeholder
# 30-day dollar volume from Binance perp klines
import glob
def ts(x): x=np.asarray(x,dtype='int64'); return np.where(x>1e14,x//1000,x)
V={}
for s in F.columns:
    k=pd.read_csv(f'carry/raw/{s}_perp.csv',header=None); d=pd.to_datetime(ts(k[0])//86400000*86400000,unit='ms')
    V[s]=pd.Series((k[4]*k[5]).values,index=d).groupby(level=0).last()
DV=pd.DataFrame(V).reindex(F.index).rolling(30,min_periods=20).mean().shift(1)
allF,allP=F.copy(),P.copy()
for N in (10,15,20,35):
    mask=DV.rank(axis=1,ascending=False)<=N          # causal: yesterday's 30-day volume rank
    F=allF.where(mask); P=allP; R=P.pct_change(); vol=R.rolling(30,min_periods=20).std()*math.sqrt(365)
    run(lb=7,hold=7,q=0.33,name=f'factor on daily top-{N} by volume')
