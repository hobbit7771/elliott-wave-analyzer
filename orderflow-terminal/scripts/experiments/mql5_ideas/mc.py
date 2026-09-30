# MQL5 idea (Portfolio Correlation Analyzer): drawdown compression across sleeves + Monte-Carlo reshuffle of returns.
import math, numpy as np, pandas as pd, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('port3.py').read().split("print('correlations")[0])   # T, C, X, idx
book=T+0.5*X; v=book.rolling(30,min_periods=20).std().shift(1); ref=book.expanding(180).std().shift(1)
P=book*(ref/v).clip(upper=1).fillna(1)+C          # site portfolio: vol-managed directional + carry
def mdd(r): eq=(1+r).cumprod(); return (1-eq/eq.cummax()).max()
parts={'trend':T,'carry':C,'factor(½)':0.5*X}
s=sum(mdd(r) for r in parts.values())
print('max DD parts:', {k:round(mdd(r)*100,1) for k,r in parts.items()}, 'sum', round(s*100,1), '% | portfolio', round(mdd(P)*100,1), '% | compression', round(s/mdd(P),2))
rng=np.random.default_rng(0)
for name,block in (('days shuffled (iid)',1),('20-day blocks',20),('monthly blocks',30)):
    r=P.values; n=len(r); dds=[]
    for _ in range(3000):
        if block==1: x=rng.permutation(r)
        else:
            k=n//block+1; starts=rng.integers(0,n-block,k); x=np.concatenate([r[s0:s0+block] for s0 in starts])[:n]
        eq=np.cumprod(1+x); dds.append((1-eq/np.maximum.accumulate(eq)).max())
    dds=np.array(dds)
    print(f"{name:20s} maxDD p50 {np.percentile(dds,50)*100:5.1f}%  p90 {np.percentile(dds,90)*100:5.1f}%  p95 {np.percentile(dds,95)*100:5.1f}%  p99 {np.percentile(dds,99)*100:5.1f}%   historical {mdd(P)*100:.1f}% is at p{(dds<mdd(P)).mean()*100:.0f}")
