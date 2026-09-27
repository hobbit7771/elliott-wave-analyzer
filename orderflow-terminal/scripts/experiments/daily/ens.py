# Zarattini/Pagani/Barbon (2025) ensemble Donchian, long/flat, vol-targeted. Signals at day close, executed next day's open.
import sys; from dbt import *
D=load(); COST=FEE_T+SLIP  # one-way cost fraction of notional
LB=[5,10,20,30,60,90,150,250,360]
def ens_signal(df, lbs=LB, exit_mode='mid'):
    c=df.c.values; n=len(c); S=np.zeros(n)
    for N in lbs:
        mx=pd.Series(c).rolling(N).max().shift(1).values; mn=pd.Series(c).rolling(N).min().shift(1).values
        mx0=pd.Series(c).rolling(N).max().values; mn0=pd.Series(c).rolling(N).min().values
        pos=0; stop=-np.inf; p=np.zeros(n)
        for i in range(N,n):
            if pos==0 and c[i]>mx[i]: pos=1; stop=(mx0[i]+mn0[i])/2
            elif pos==1:
                stop=max(stop,(mx0[i]+mn0[i])/2)
                if c[i]<stop: pos=0
            p[i]=pos
        S+=p
    return S/len(lbs)
def run(syms, lbs=LB, tgt=0.25, volwin=90, cap=1.0, btc_filter=False, start=None, fund=FUND_LONG, verbose=True, name=''):
    R={}; W={}
    btc=D['BTC']; bf=(btc.c>btc.c.rolling(50).mean())
    for s in syms:
        df=D[s]; r=df.o.pct_change().shift(-2)  # open(i+1)->open(i+2): held by a decision at close i
        sig=pd.Series(ens_signal(df,lbs),index=df.index)
        vol=df.c.pct_change().rolling(volwin).std()*math.sqrt(365)
        w=(sig*(tgt/vol).clip(upper=cap)).fillna(0)
        if btc_filter: w=w*bf.reindex(df.index).fillna(False).astype(float)
        # w decided at close i, held from open i+1 to open i+2 -> return on day i+1 row
        ret=w*r - (w.diff().abs().fillna(w.abs()))*COST - w*fund
        R[s]=ret.shift(1); W[s]=w
    R=pd.DataFrame(R); W=pd.DataFrame(W)
    avail=R.notna().sum(axis=1).replace(0,np.nan)
    P=(R.fillna(0).sum(axis=1)/avail).fillna(0)   # equal capital per available coin
    if start: P=P[P.index>=start]
    ann=P.mean()*365; vol=P.std()*math.sqrt(365); sr=ann/vol; eq=(1+P).cumprod(); mdd=(1-eq/eq.cummax()).max()
    cagr=eq.iloc[-1]**(365/len(P))-1
    yrs=P.groupby(P.index.year).apply(lambda x:(1+x).prod()-1)
    if verbose: print(f"{name:40s} CAGR {cagr*100:+6.1f}% vol {vol*100:4.1f}% SR {sr:.2f} maxDD {mdd*100:4.1f}% expo {W.mean().mean():.2f} | "+' '.join(f"{y}:{v*100:+.0f}%" for y,v in yrs.items()))
    return P,dict(sr=sr,cagr=cagr,mdd=mdd)
def bh(syms,start=None,name='buy&hold equal weight'):
    R=pd.DataFrame({s:D[s].o.pct_change().shift(-1).shift(1) for s in syms}); avail=R.notna().sum(axis=1).replace(0,np.nan)
    P=(R.fillna(0).sum(axis=1)/avail).fillna(0)
    if start: P=P[P.index>=start]
    ann=P.mean()*365; vol=P.std()*math.sqrt(365); eq=(1+P).cumprod(); print(f"{name:40s} CAGR {(eq.iloc[-1]**(365/len(P))-1)*100:+6.1f}% vol {vol*100:4.1f}% SR {ann/vol:.2f} maxDD {(1-eq/eq.cummax()).max()*100:4.1f}%")
    return P
if __name__=='__main__':
    ALL=list(D); print('coins',ALL, D['BTC'].index[0].date(), D['BTC'].index[-1].date())
    bh(['BTC'],name='BTC buy&hold'); bh(ALL)
    run(['BTC'],name='ensemble BTC'); run(['ETH'],name='ensemble ETH')
    P,_=run(ALL,name='ensemble all 17 (paper rules)')
    run(ALL,btc_filter=True,name='ensemble all + BTC>SMA50')
    run(ALL,lbs=[20],name='single 20-day midline')
    run(ALL,lbs=[20,30,60,90,150,250,360],name='ensemble no 5/10')
    run(ALL,tgt=0.5,cap=2,name='ensemble tgt 50% cap2')
    run(ALL,fund=0.0001,name='ensemble funding 0.01%/day')
    for a,b in [('2021','2023-06-01'),('2023-06-01','2027')]:
        Q=P[(P.index>=a)&(P.index<b)]; print(f"  subperiod {a}..{b}: SR {Q.mean()/Q.std()*math.sqrt(365):.2f}")
    print('bootstrap p', block_boot_p(P[P.index>='2022']))
