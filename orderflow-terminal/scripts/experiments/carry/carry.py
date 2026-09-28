# Delta-neutral cash-and-carry: long spot + short USDⓈ-M perpetual, rotating into the coins with the highest funding.
import sys, glob, math, numpy as np, pandas as pd
def ts(x): x=np.asarray(x,dtype='int64'); return np.where(x>1e14,x//1000,x)   # spot archive moved to µs in 2025
def day(x): return pd.to_datetime(ts(x)//86400000*86400000,unit='ms')
S=sorted({f.split('/')[-1].split('_')[0] for f in glob.glob('raw/*_fund.csv')})
F={}; SP={}; PP={}
for s in S:
    try:
        f=pd.read_csv(f'raw/{s}_fund.csv',header=None); sp=pd.read_csv(f'raw/{s}_spot.csv',header=None); pp=pd.read_csv(f'raw/{s}_perp.csv',header=None)
    except FileNotFoundError: continue
    F[s]=f.groupby(day(f[0]))[2].sum()                 # funding per UTC day (sum of settlements)
    SP[s]=pd.Series(sp[4].values,index=day(sp[0])).groupby(level=0).last()
    PP[s]=pd.Series(pp[4].values,index=day(pp[0])).groupby(level=0).last()
F=pd.DataFrame(F).sort_index(); SP=pd.DataFrame(SP).reindex(F.index); PP=pd.DataFrame(PP).reindex(F.index)
F=F[F.index>='2020-01-01']; SP=SP.loc[F.index]; PP=PP.loc[F.index]
basis_ret=(SP.pct_change()-PP.pct_change())             # long spot, short perp, per unit notional (daily rebalanced)
ok=SP.notna()&PP.notna()&F.notna()
def run(entry=0.15, exit=0.05, K=5, lb=7, cost_side=0.0015, name='', hold_min=0, only=None, verbose=True):
    ann=F.rolling(lb,min_periods=lb).mean()*365             # trailing annualized funding, known at the close of day t
    cols=only or list(F.columns)
    W=pd.DataFrame(0.0,index=F.index,columns=F.columns); held=set()
    for i,t in enumerate(F.index):
        a=ann.loc[t,cols]; o=ok.loc[t,cols]
        held={s for s in held if o[s] and a[s]>exit}
        cand=a[o&(a>entry)].drop(list(held),errors='ignore').sort_values(ascending=False)
        for s in cand.index:
            if len(held)>=K: break
            held.add(s)
        for s in held: W.at[t,s]=1.0/K                       # equal notional slots; unused slots stay in cash
    Wd=W.shift(1).fillna(0)                                  # decided at close t, held during day t+1
    pnl=(Wd*(F.fillna(0)+basis_ret.fillna(0))).sum(axis=1) - W.diff().abs().fillna(W).sum(axis=1).shift(1).fillna(0)*cost_side
    # returns on capital: notional N needs N in spot + ~0.5 N perp margin -> capital 1.5 N
    r=pnl/1.5
    eq=(1+r).cumprod(); yrs=len(r)/365; cagr=eq.iloc[-1]**(1/yrs)-1; vol=r.std()*math.sqrt(365); sr=r.mean()*365/vol if vol>0 else 0
    dd=(1-eq/eq.cummax()).max(); expo=Wd.sum(axis=1).mean()
    by=r.groupby(r.index.year).apply(lambda x:(1+x).prod()-1)
    if verbose: print(f"{name:44s} CAGR {cagr*100:+6.1f}% vol {vol*100:4.1f}% SR {sr:5.2f} maxDD {dd*100:4.1f}% exposure {expo:.2f} | "+' '.join(f"{y}:{v*100:+.1f}%" for y,v in by.items()))
    return r
if __name__=='__main__':
    print('coins',len(F.columns),F.index[0].date(),'..',F.index[-1].date())
    run(entry=-1,exit=-2,K=2,only=['BTC','ETH'],name='always BTC+ETH carry (classic)')
    for e,x in ((0.10,0.03),(0.15,0.05),(0.20,0.08),(0.30,0.10)):
        for K in (3,5,10):
            run(entry=e,exit=x,K=K,name=f'rotate: enter>{e:.0%} exit<{x:.0%} K={K}')
    run(entry=0.15,exit=0.05,K=5,cost_side=0.0008,name='enter>15% K=5, maker-ish costs 0.08%/side')
    run(entry=0.15,exit=0.05,K=5,lb=3,name='enter>15% K=5, lookback 3 d')
    run(entry=0.15,exit=0.05,K=5,lb=14,name='enter>15% K=5, lookback 14 d')
