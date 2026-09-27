import warnings; warnings.filterwarnings('ignore')
import sys, math, numpy as np, pandas as pd
H=int(sys.argv[1]) if len(sys.argv)>1 else 5
R=pd.read_pickle(f'mlpred_h{H}.pkl'); Pn=pd.read_pickle('panel.pkl')
COST=0.00055+0.0002
ret1=Pn.reset_index().pivot(index='t',columns='sym',values='y1')          # open(i+1)->open(i+2), decided at close i
fund=Pn.reset_index().pivot(index='t',columns='sym',values='f1')/1e4
vol=Pn.reset_index().pivot(index='t',columns='sym',values='vol30')*math.sqrt(365)
def rep(name,W):
    W=W.reindex(ret1.index).fillna(0); W=W.loc[W.index>=R.index.min()]
    r=ret1.reindex(W.index); f=fund.reindex(W.index).fillna(0.0003)
    pnl=(W*r.fillna(0)).sum(axis=1)-W.diff().abs().sum(axis=1)*COST-(W*f).sum(axis=1)
    ann=pnl.mean()*365; v=pnl.std()*math.sqrt(365); eq=pnl.cumsum(); dd=(eq.cummax()-eq).max()
    h1=pnl[pnl.index<'2024-06-01']; h2=pnl[pnl.index>='2024-06-01']; s=lambda x:x.mean()/x.std()*math.sqrt(365)
    yrs=pnl.groupby(pnl.index.year).sum()
    print(f"{name:44s} SR {ann/v:5.2f} ann {ann*100:+6.1f}% vol {v*100:4.1f}% maxDD {dd*100:5.1f}% turnover/day {W.diff().abs().sum(axis=1).mean():.2f} | halves {s(h1):+.2f}/{s(h2):+.2f} | "+' '.join(f"{y}:{x*100:+.0f}%" for y,x in yrs.items()))
    return pnl
def score(col, smooth):
    S=R.reset_index().pivot(index='t',columns='sym',values=col)
    return S.rolling(smooth,min_periods=1).mean()
iv=(0.25/vol).clip(upper=1)   # vol-target weights per coin
base=(iv.notna()&ret1.notna()).astype(float)
bh=rep('equal-weight long all (vol-scaled), benchmark',(iv*base).div(base.sum(axis=1),axis=0))
for col in ('ridge','logit','ens','etr'):
    for sm in (1,5):
        S=score(col+'_z' if col!='ens' else 'ens',sm); rk=S.rank(axis=1,pct=True); n=S.notna().sum(axis=1)
        L=(rk>=2/3).astype(float); Sh=(rk<=1/3).astype(float)
        wL=(L*iv).div(L.sum(axis=1),axis=0); wS=(Sh*iv).div(Sh.sum(axis=1),axis=0)
        rep(f'{col} smooth{sm}: long top1/3 (long-only)',wL)
        rep(f'{col} smooth{sm}: long-short top/bottom 1/3',wL-wS)
# time-series: long/flat when ridge prediction > 0
for col in ('ridge','logit'):
    S=score(col,5); W=(S>0).astype(float)*iv; rep(f'TS {col}: long if pred>0, vol-target, /N',W.div(base.sum(axis=1),axis=0))
