from ens import *
ALL=list(D)
C=pd.DataFrame({s:D[s].c for s in ALL}); O=pd.DataFrame({s:D[s].o for s in ALL})
R=O.pct_change().shift(-2)  # held from open i+1 to open i+2 by decision at close i
V=C.pct_change().rolling(90).std()*math.sqrt(365)
def xs(look=28, top=0.33, hold=7, long_short=False, trend_gate=False, name=''):
    mom=C/C.shift(look)-1; W=pd.DataFrame(0.0,index=C.index,columns=ALL); cur=None
    for k,i in enumerate(C.index):
        if k%hold==0:
            m=mom.loc[i].dropna()
            if len(m)>=6:
                q=m.rank(pct=True); w=pd.Series(0.0,index=ALL)
                longs=q[q>=1-top].index
                if trend_gate: longs=[s for s in longs if m[s]>0]
                w[longs]=1.0
                if long_short: w[q[q<=top].index]=-1.0
                w=w*(0.25/V.loc[i]).clip(upper=1).fillna(0); n=(w!=0).sum(); cur=w/max(n,1)
        if cur is not None: W.loc[i]=cur
    ret=(W*R).sum(axis=1)-W.diff().abs().sum(axis=1)*COST-W.clip(lower=0).sum(axis=1)*FUND_LONG
    P=ret.shift(1).fillna(0); P=P[P.index>='2021-06-01']
    ann=P.mean()*365; vol=P.std()*math.sqrt(365); eq=(1+P).cumprod()
    h1=P[P.index<'2023-06-01']; h2=P[P.index>='2023-06-01']
    print(f"{name:34s} SR {ann/vol:5.2f} CAGR {(eq.iloc[-1]**(365/len(P))-1)*100:+6.1f}% maxDD {(1-eq/eq.cummax()).max()*100:4.1f}% | halves SR {h1.mean()/h1.std()*19.1:+.2f} / {h2.mean()/h2.std()*19.1:+.2f}")
    return P
for look in (7,14,28,56):
    xs(look,name=f'XS mom top1/3 look {look}d')
    xs(look,trend_gate=True,name=f'XS mom top1/3 +abs>0 look {look}d')
xs(28,long_short=True,name='XS mom long-short 28d')
