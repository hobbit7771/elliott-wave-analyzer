# Candlestick patterns and ZigZag wave structure: do they predict reversals? Daily bars, 17 Bybit perps, 2021-2026.
import warnings; warnings.filterwarnings('ignore')
import math, numpy as np, pandas as pd
from dbt import load, atr
D=load()
def candles(df):
    o,h,l,c=df.o,df.h,df.l,df.c; A=atr(df,14); rng=(h-l).replace(0,np.nan); body=(c-o).abs()
    up=h-np.maximum(o,c); lo=np.minimum(o,c)-l
    p=pd.DataFrame(index=df.index)
    p['bull_engulf']=(c>o)&(c.shift()<o.shift())&(c>=o.shift())&(o<=c.shift())&(body>body.shift())
    p['bear_engulf']=(c<o)&(c.shift()>o.shift())&(c<=o.shift())&(o>=c.shift())&(body>body.shift())
    p['hammer']=(lo>=2*body)&(up<=0.3*rng)&(rng>0.8*A)          # long lower wick
    p['shooting_star']=(up>=2*body)&(lo<=0.3*rng)&(rng>0.8*A)   # long upper wick
    p['doji']=(body<=0.1*rng)&(rng>0.6*A)
    p['inside']=(h<h.shift())&(l>l.shift())
    p['morning_star']=(c.shift(2)<o.shift(2))&((c.shift(2)-o.shift(2)).abs()>0.6*A)&((c.shift()-o.shift()).abs()<0.3*A)&(c>o)&(c>(o.shift(2)+c.shift(2))/2)
    p['evening_star']=(c.shift(2)>o.shift(2))&((c.shift(2)-o.shift(2)).abs()>0.6*A)&((c.shift()-o.shift()).abs()<0.3*A)&(c<o)&(c<(o.shift(2)+c.shift(2))/2)
    return p
def context(df):
    c=df.c; x=pd.DataFrame(index=df.index)
    x['down10']=c<c.shift(10)                                     # "after a decline" (bullish patterns are meant for this)
    x['up10']=c>c.shift(10)
    return x
# forward outcome: entry at next open; +1 if +2 ATR before -1 ATR (long), measured both ways, 10-day limit (triple barrier)
def barrier(df, up=2.0, dn=1.0, H=10):
    o,h,l=df.o.values,df.h.values,df.l.values; A=atr(df,14).values; n=len(df); resL=np.full(n,np.nan); resS=np.full(n,np.nan); fwd=np.full(n,np.nan)
    for i in range(20,n-H-1):
        e=o[i+1]; a=A[i]; tl=e+up*a; sl=e-dn*a; ts=e-up*a; ss=e+dn*a; rl=rs=None
        for j in range(i+1,i+1+H):
            if rl is None:
                if l[j]<=sl: rl=-dn
                elif h[j]>=tl: rl=up
            if rs is None:
                if h[j]>=ss: rs=-dn
                elif l[j]<=ts: rs=up
        resL[i]=rl if rl is not None else (df.c.values[i+H]-e)/a
        resS[i]=rs if rs is not None else (e-df.c.values[i+H])/a
        fwd[i]=(df.c.values[min(n-1,i+5)]-e)/a
    return pd.DataFrame({'L':resL,'S':resS,'f5':fwd},index=df.index)
rows=[]
for s,df in D.items():
    p=candles(df); x=context(df); b=barrier(df)
    rows.append(pd.concat([p,x,b],axis=1).assign(sym=s))
X=pd.concat(rows).dropna(subset=['L'])
X=X[X.index>='2021-06-01']
base_L=X.L.mean(); base_S=X.S.mean()
print(f"all days: n={len(X)}  long 2:1 barrier avg {base_L:+.3f} ATR, short {base_S:+.3f} ATR (costs not included)")
print(f"{'pattern':28s} {'n':>5s} {'long avgATR':>11s} {'vs all':>7s} {'short avgATR':>12s} {'vs all':>7s}  5d fwd ATR   t(long-all)")
for pat,side,ctx in [('bull_engulf','L','down10'),('hammer','L','down10'),('morning_star','L','down10'),('doji','L','down10'),('inside','L',None),
                     ('bear_engulf','S','up10'),('shooting_star','S','up10'),('evening_star','S','up10'),('doji','S','up10')]:
    m=X[pat].fillna(False).astype(bool)
    if ctx: m&=X[ctx].fillna(False).astype(bool)
    g=X[m]; v=g[side]; base=base_L if side=='L' else base_S
    t=(v.mean()-base)/(X[side].std()/math.sqrt(max(1,len(v))))
    print(f"{pat+(' after '+ctx if ctx else ''):28s} {len(g):5d} {g.L.mean():+11.3f} {g.L.mean()-base_L:+7.3f} {g.S.mean():+12.3f} {g.S.mean()-base_S:+7.3f}  {g.f5.mean():+9.3f}   {t:+.2f} ({'long' if side=='L' else 'short'})")
X.to_pickle('pat.pkl')
