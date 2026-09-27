# Daily research backtester: 17 Bybit perps, 2021-2026. Orders from closed days, executed on later days,
# stop first when a day touches stop and target; Bybit taker fees + stop slippage; funding estimate for longs.
import json, glob, os, numpy as np, pandas as pd, math
FEE_T=0.00055; FEE_M=0.0002; SLIP=0.0002; FUND_LONG=0.0003  # per day, longs pay (conservative), shorts 0
def load():
    D={}
    for f in sorted(glob.glob('daily/*.json')):
        a=np.array(json.load(open(f))); s=os.path.basename(f)[:-5].replace('USDT','')
        df=pd.DataFrame(a[:,:6],columns=['t','o','h','l','c','v']); df.index=pd.to_datetime(df.t,unit='ms'); D[s]=df.drop(columns='t')
    return D
def atr(df,n=14):
    pc=df.c.shift(); tr=pd.concat([df.h-df.l,(df.h-pc).abs(),(df.l-pc).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False).mean()
def adx(df,n=14):
    up=df.h.diff(); dn=-df.l.diff()
    pdm=np.where((up>dn)&(up>0),up,0.0); mdm=np.where((dn>up)&(dn>0),dn,0.0)
    pc=df.c.shift(); tr=pd.concat([df.h-df.l,(df.h-pc).abs(),(df.l-pc).abs()],axis=1).max(axis=1)
    a=tr.ewm(alpha=1/n,adjust=False).mean()
    pdi=100*pd.Series(pdm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/a
    mdi=100*pd.Series(mdm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/a
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/n,adjust=False).mean(), pdi, mdi

class Trade:
    __slots__=('sym','d','entry','stop','t0','t1','exit','r','why')
def simulate(df, signals, sym, max_hold, trail=None):
    """signals: list of dict(day_index_created, dir, entry_stop, stop, target, valid_days, why). Executes on later days.
    trail: None or ('chandelier', k, n) or ('donchian', n) exits. Returns trades and a daily R pnl series (MTM)."""
    o,h,l,c=df.o.values,df.h.values,df.l.values,df.c.values; A=atr(df).values
    n=len(df); pnl=np.zeros(n); trades=[]; busy_until=-1
    for s in sorted(signals,key=lambda x:x['i']):
        i0=s['i']
        if i0+1>=n or i0+1<=busy_until: continue
        d=s['dir']; filled=None
        for j in range(i0+1,min(n,i0+1+s['valid'])):
            if d>0 and h[j]>=s['entry']: filled=(j,max(s['entry'],o[j])); break
            if d<0 and l[j]<=s['entry']: filled=(j,min(s['entry'],o[j])); break
        if not filled: continue
        j,px=filled; stop=s['stop']; risk=(px-stop)*d
        if risk<=0: continue
        tgt=s.get('target'); cost=(FEE_T+FEE_T+SLIP)*px/risk
        prev=px; k=j; exitpx=None
        while k<n:
            # stop first (on the entry day too: conservative)
            if (d>0 and l[k]<=stop) or (d<0 and h[k]>=stop):
                ex=stop if not ((d>0 and o[k]<stop) or (d<0 and o[k]>stop)) or k==j else o[k]
                exitpx=ex; pnl[k]+=(ex-prev)*d/risk; break
            if tgt is not None and k>j and ((d>0 and h[k]>=tgt) or (d<0 and l[k]<=tgt)):
                exitpx=tgt; pnl[k]+=(tgt-prev)*d/risk; cost-= (FEE_T+SLIP-FEE_M)*px/risk; break
            if k-j+1>=max_hold:
                exitpx=c[k]; pnl[k]+=(c[k]-prev)*d/risk; cost-=SLIP*px/risk; break
            pnl[k]+=(c[k]-prev)*d/risk; prev=c[k]
            if d>0: pnl[k]-=FUND_LONG*px/risk
            # trailing exits computed on the CLOSED day k, active from k+1
            if trail:
                if trail[0]=='chandelier':
                    hh=h[j:k+1].max() if d>0 else l[j:k+1].min()
                    ts=hh-d*trail[1]*A[k]
                    stop=max(stop,ts) if d>0 else min(stop,ts)
                elif trail[0]=='mid':
                    w=trail[1]; ts=(h[max(0,k-w+1):k+1].max()+l[max(0,k-w+1):k+1].min())/2
                    stop=max(stop,ts) if d>0 else min(stop,ts)
                elif trail[0]=='donchian':
                    w=trail[1]; ts=l[max(0,k-w+1):k+1].min() if d>0 else h[max(0,k-w+1):k+1].max()
                    stop=max(stop,ts) if d>0 else min(stop,ts)
            k+=1
        if exitpx is None: continue  # still open at the end: dropped
        # costs booked on the exit day
        pnl[k]-=cost
        t=Trade(); t.sym=sym; t.d=d; t.entry=px; t.stop=s['stop']; t.t0=df.index[j]; t.t1=df.index[k]; t.exit=exitpx
        t.r=(exitpx-px)*d/risk-cost-(FUND_LONG*px/risk*(k-j) if d>0 else 0); t.why=s['why']; trades.append(t)
        busy_until=k
    return trades, pd.Series(pnl,index=df.index)

# ---------------- strategies (signal generators on CLOSED days) ----------------
def holy_grail(df, adx_min=30, ema_n=20, clean=5, valid=3, off=0.02, long_only=False, btc_ok=None):
    e=df.c.ewm(span=ema_n,adjust=False).mean(); ax,pdi,mdi=adx(df); A=atr(df)
    h,l=df.h.values,df.l.values; sig=[]
    for i in range(60,len(df)):
        if not ax.iloc[i]>adx_min: continue
        for d in (1,-1):
            if long_only and d<0: continue
            if d>0 and not pdi.iloc[i]>mdi.iloc[i]: continue
            if d<0 and not mdi.iloc[i]>pdi.iloc[i]: continue
            touch = l[i]<=e.iloc[i] if d>0 else h[i]>=e.iloc[i]
            ok = all((l[k]>e.iloc[k]) if d>0 else (h[k]<e.iloc[k]) for k in range(i-clean,i))
            if not(touch and ok): continue
            if btc_ok is not None and not btc_ok(df.index[i],d): continue
            tgt=h[i-20:i].max() if d>0 else l[i-20:i].min()
            ent=h[i]+off*A.iloc[i] if d>0 else l[i]-off*A.iloc[i]
            if (tgt-ent)*d<=0: continue
            sig.append(dict(i=i,dir=d,entry=ent,stop=l[i]-0.01*A.iloc[i] if d>0 else h[i]+0.01*A.iloc[i],target=tgt,valid=valid,why='HG'))
    return sig
def donchian(df, n_in=55, stop_atr=2.0, long_only=False, btc_ok=None, er_min=None, er_n=20):
    A=atr(df,20); hh=df.h.rolling(n_in).max().shift(1); ll=df.l.rolling(n_in).min().shift(1); sig=[]
    er=(df.c-df.c.shift(er_n)).abs()/(df.c.diff().abs().rolling(er_n).sum())
    for i in range(n_in+1,len(df)-1):
        for d in (1,-1):
            if long_only and d<0: continue
            if er_min is not None and not er.iloc[i]>=er_min: continue
            if btc_ok is not None and not btc_ok(df.index[i],d): continue
            lvl=hh.iloc[i] if d>0 else ll.iloc[i]
            # the order for day i+1 is a stop at the channel extreme of the days up to i (known at i's close)
            ch=df.h.iloc[i-n_in+1:i+1].max() if d>0 else df.l.iloc[i-n_in+1:i+1].min()
            if d>0 and df.c.iloc[i]>=ch: continue
            if d<0 and df.c.iloc[i]<=ch: continue
            ent=ch; st=ent-d*stop_atr*A.iloc[i]
            sig.append(dict(i=i,dir=d,entry=ent,stop=st,target=None,valid=1,why='DON'))
    return sig

# ---------------- metrics ----------------
def metrics(p):
    p=p.fillna(0); 
    if p.abs().sum()==0: return dict(sr=0,ann=0,mdd=0,t=0)
    ann=p.mean()*365; vol=p.std()*math.sqrt(365); sr=ann/vol if vol>0 else 0
    eq=p.cumsum(); mdd=(eq.cummax()-eq).max()
    return dict(sr=sr,ann=ann,mdd=mdd)
def block_boot_p(p, B=2000, block=20, seed=1):
    """stationary block bootstrap: P(mean <= 0) under resampling of the centred series (one-sided p-value)."""
    x=p.fillna(0).values; n=len(x); rng=np.random.default_rng(seed); m=x.mean(); xc=x-m; cnt=0
    for _ in range(B):
        idx=[]; 
        while len(idx)<n:
            s=rng.integers(0,n); L=rng.geometric(1/block); idx.extend(((s+np.arange(L))%n).tolist())
        if xc[idx[:n]].mean()>=m: cnt+=1
    return (cnt+1)/(B+1)
def deflated_sr(sr_ann, n_obs_days, n_trials, skew=0, kurt=3, sr_var_trials=None):
    """Bailey & López de Prado (2014): probability that the true SR > the expected max SR of n_trials noise strategies."""
    from scipy.stats import norm
    sr=sr_ann/math.sqrt(365)  # daily
    emc=0.5772156649
    v=sr_var_trials if sr_var_trials is not None else 1/n_obs_days
    sr0=math.sqrt(v)*((1-emc)*norm.ppf(1-1/n_trials)+emc*norm.ppf(1-1/(n_trials*math.e))) if n_trials>1 else 0
    z=(sr-sr0)*math.sqrt(n_obs_days-1)/math.sqrt(1-skew*sr+(kurt-1)/4*sr*sr)
    return norm.cdf(z), sr0*math.sqrt(365)
