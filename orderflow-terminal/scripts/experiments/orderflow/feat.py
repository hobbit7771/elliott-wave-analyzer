# 1-second order-flow bars -> features sampled every STEP seconds (only data up to t) + forward returns.
import glob, os, sys, numpy as np, pandas as pd
import os
STEP=int(os.environ.get('STEP',5)); HZ=tuple(int(x) for x in os.environ.get('HZ','10,30,60,300').split(','))
def load_day(sym,d):
    z=np.load(f"{os.environ.get('SEC','sec')}/{sym}_{d}.npz"); s=z['s']
    t0=(s[0]//86400)*86400; idx=np.arange(t0,t0+86400)
    df=pd.DataFrame({k:z[k] for k in ('o','h','l','c','bq','sq','bn','sn','mx','vw')},index=s).reindex(idx)
    df['c']=df.c.ffill().bfill(); 
    for k in ('o','h','l','vw'): df[k]=df[k].fillna(df.c)
    for k in ('bq','sq','bn','sn','mx'): df[k]=df[k].fillna(0)
    dep=None
    if len(z['ds']):
        dep=pd.DataFrame(z['dv'],index=z['ds'],columns=z['dcols']).sort_index()
    return df,dep
def build(sym, days):
    parts=[]; prev=None
    for d in days:
        if not os.path.exists(f"{os.environ.get('SEC','sec')}/{sym}_{d}.npz"): prev=None; continue
        df,dep=load_day(sym,d)
        # prepend the previous day's last hour so rolling windows are warm at 00:00
        full=pd.concat([prev,df]) if prev is not None else df
        c=full.c; lc=np.log(c); r1=lc.diff().fillna(0)
        q=full.bq+full.sq; nt=full.bq-full.sq; n=full.bn+full.sn; px=c
        F=pd.DataFrame(index=full.index)
        rv=r1.rolling(900,min_periods=60).std().replace(0,np.nan)
        F['rv900']=np.log(rv); F['rv60']=np.log(r1.rolling(60).std().replace(0,np.nan)/rv)
        for w in (1,5,10,30,60,300):
            F[f'ofi{w}']=nt.rolling(w).sum()/q.rolling(w).sum().replace(0,np.nan)
        for w in (10,60):
            F[f'cnt{w}']=(full.bn-full.sn).rolling(w).sum()/n.rolling(w).sum().replace(0,np.nan)
        base_n=n.rolling(1800,min_periods=300).mean()*10; F['int10']=np.log((n.rolling(10).sum()+1)/(base_n+1))
        base_q=(q*px).rolling(1800,min_periods=300).mean()*60; F['vol60']=np.log(((q*px).rolling(60).sum()+1)/(base_q+1))
        F['big10']=np.log1p(full.mx.rolling(10).max()/((q*px).rolling(1800,min_periods=300).mean()+1))
        # signed notional of the last 60s in units of normal 60s volume
        F['nofi60']=(nt*px).rolling(60).sum()/(base_q+1)
        for w in (1,5,10,30,60,300,900):
            F[f'r{w}']=(lc-lc.shift(w))/(rv*np.sqrt(w))
        hi=full.h.rolling(300).max(); lo=full.l.rolling(300).min(); F['pos300']=(c-lo)/(hi-lo).replace(0,np.nan)
        F['vwdev']=(lc-np.log(full.vw.rolling(60).mean()))/rv
        if dep is not None and len(dep):
            dd=dep.reindex(full.index,method='ffill')
            for p in (1.0,2.0,5.0):
                if p in dd.columns and -p in dd.columns:
                    F[f'dimb{int(p)}']=(dd[-p]-dd[p])/(dd[-p]+dd[p])
            if 1.0 in dd.columns and -1.0 in dd.columns:
                imb=(dd[-1.0]-dd[1.0])/(dd[-1.0]+dd[1.0]); F['dimb1_ch']=imb-imb.shift(300)
        hour=(full.index%86400)/3600; F['hs']=np.sin(2*np.pi*hour/24); F['hc']=np.cos(2*np.pi*hour/24)
        for h in HZ:
            F[f'y{h}']=lc.shift(-h)-lc          # from the last trade price at t to that at t+h
            F[f'e{h}']=lc.shift(-h-1)-lc.shift(-1)  # executable: enter 1 s later, exit h s after entry
        F['px']=c; F['rvraw']=rv
        F=F.loc[df.index]; F=F.iloc[::STEP]; F['day']=d
        parts.append(F); prev=df.iloc[-3600:]
    X=pd.concat(parts); X['sym']=sym; return X
if __name__=='__main__':
    files=sorted(glob.glob(os.environ.get('SEC','sec')+'/*.npz')); days=sorted({f.split('_')[1][:-4] for f in files})
    out={}
    for sym in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        out[sym]=build(sym,days); print(sym,out[sym].shape,flush=True)
    # BTC lead features for the alts
    b=out['BTCUSDT'][['ofi10','ofi60','r10','r30','r60','nofi60']].add_prefix('btc_')
    for sym in ('ETHUSDT','SOLUSDT'): out[sym]=out[sym].join(b)
    for c in b.columns: out['BTCUSDT'][c]=np.nan
    X=pd.concat(out.values()); X.to_pickle(os.environ.get('OUT','feat.pkl')); print(X.shape, X.day.nunique(),'days')
