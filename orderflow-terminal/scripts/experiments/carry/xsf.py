# Cross-sectional funding factor on Binance perps: long the lowest-funding coins, short the highest-funding ones.
import math, numpy as np, pandas as pd, os
os.chdir('carry'); g={}; exec(open('carry.py').read().split("if __name__")[0],g); os.chdir('..')
F=g['F']; P=g['PP']; R=P.pct_change()
vol=R.rolling(30,min_periods=20).std()*math.sqrt(365)
def run(lb=7, q=0.2, hold=7, cost=0.00075, beta_neutral=True, name='', start='2021-01-01'):
    sig=F.rolling(lb,min_periods=lb).mean()
    W=pd.DataFrame(0.0,index=F.index,columns=F.columns); cur=None
    for i,t in enumerate(F.index):
        if i%hold==0:
            s=sig.loc[t].dropna(); s=s[R.loc[:t].tail(30).notna().sum()[s.index]>=20]
            if len(s)>=10:
                rk=s.rank(pct=True); L=rk[rk<=q].index; S=rk[rk>=1-q].index
                iv=(0.2/vol.loc[t]).clip(upper=2)
                w=pd.Series(0.0,index=F.columns); w[L]=iv[L]/iv[L].sum(); w[S]=-iv[S]/iv[S].sum()
                cur=w*0.5                       # 0.5 gross long + 0.5 gross short
        if cur is not None: W.loc[t]=cur
    Wd=W.shift(1).fillna(0)
    # longs pay funding, shorts receive it: -w * funding
    pnl=(Wd*R.fillna(0)).sum(axis=1)-(Wd*F.fillna(0)).sum(axis=1)-W.diff().abs().sum(axis=1).shift(1).fillna(0)*cost
    carry_part=-(Wd*F.fillna(0)).sum(axis=1); price_part=(Wd*R.fillna(0)).sum(axis=1)
    r=pnl[pnl.index>=start]; eq=(1+r).cumprod(); yrs=len(r)/365; cagr=eq.iloc[-1]**(1/yrs)-1; v=r.std()*math.sqrt(365); dd=(1-eq/eq.cummax()).max()
    h1=r[r.index<'2023-09-01']; h2=r[r.index>='2023-09-01']; s=lambda x:x.mean()/x.std()*math.sqrt(365)
    by=r.groupby(r.index.year).apply(lambda x:(1+x).prod()-1)
    print(f"{name:34s} CAGR {cagr*100:+6.1f}% vol {v*100:4.1f}% SR {s(r):5.2f} DD {dd*100:4.1f}% halves {s(h1):+.2f}/{s(h2):+.2f} | funding {carry_part[r.index].sum()*100:+.0f}% price {price_part[r.index].sum()*100:+.0f}% | "+' '.join(f"{y}:{x*100:+.0f}%" for y,x in by.items()))
    return r
for lb in (3,7,30):
    for hold in (1,7):
        run(lb=lb,hold=hold,name=f'XS funding lb{lb} hold{hold}')
