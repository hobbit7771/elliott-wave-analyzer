import warnings; warnings.filterwarnings('ignore')
from dbt import *
D=load(); btc=D['BTC']
bs50=btc.c.rolling(50).mean(); bs200=btc.c.rolling(200).mean()
def regime(t, kind):
    if t not in btc.index: return 0
    i=btc.index.get_loc(t)
    if kind=='sma50': 
        if i<55: return 0
        return -1 if (btc.c.iloc[i]<bs50.iloc[i] and bs50.iloc[i]<bs50.iloc[i-5]) else 1
    if kind=='sma200':
        if i<200: return 0
        return 1 if btc.c.iloc[i]>bs200.iloc[i] else -1
    return 1
def gen(df, n_in=20, stop_atr=3.0, mode='stop', squeeze=None, vol_conf=None, reg='sma50', shorts=False):
    A=atr(df,20); a10=atr(df,10); a60=atr(df,60); vma=df.v.rolling(20).mean().shift(1)
    h,l,c,v=df.h.values,df.l.values,df.c.values,df.v.values; sig=[]
    for i in range(max(n_in,60)+1,len(df)-1):
        rg=regime(df.index[i],reg)
        for d in (1,-1):
            if d<0 and not shorts: continue
            if d>0 and rg<0: continue
            if d<0 and rg>=0: continue
            if squeeze is not None and not (a10.iloc[i-1]/a60.iloc[i-1] < squeeze): continue
            if mode=='stop':
                ch=h[i-n_in+1:i+1].max() if d>0 else l[i-n_in+1:i+1].min()
                if (c[i]-ch)*d>=0: continue
                sig.append(dict(i=i,dir=d,entry=ch,stop=ch-d*stop_atr*A.iloc[i],target=None,valid=1,why='S'))
            else:  # close confirmation: close beyond the prior n_in-day channel, buy next open
                ch=h[i-n_in:i].max() if d>0 else l[i-n_in:i].min()
                if not (c[i]-ch)*d>0: continue
                prevch=h[i-n_in-1:i-1].max() if d>0 else l[i-n_in-1:i-1].min()
                if (c[i-1]-prevch)*d>0: continue  # only the first close beyond
                if vol_conf is not None and not v[i]>vol_conf*vma.iloc[i]: continue
                e=0.0 if d>0 else 1e18
                sig.append(dict(i=i,dir=d,entry=e,stop=c[i]-d*stop_atr*A.iloc[i],target=None,valid=1,why='C'))
    return sig
def run(name, trail=('donchian',10), **kw):
    ps=[]; T=[]
    for s,df in D.items():
        tr,p=simulate(df,gen(df,**kw),s,10000,trail); ps.append(p.rename(s)); T+=tr
    P=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1); P=P[P.index>='2021-06-01']
    m=metrics(P); f=lambda x:x.mean()/x.std()*math.sqrt(365) if x.std()>0 else 0
    h1=P[P.index<'2023-06-01']; h2=P[P.index>='2023-06-01']; r=[t.r for t in T]
    L=[t.r for t in T if t.d>0]; S=[t.r for t in T if t.d<0]
    print(f"{name:44s} n {len(T):4d} avgR {np.mean(r):+.2f} SR {m['sr']:.2f} sumR {P.sum():+5.0f} DD {m['mdd']:4.0f} halves {f(h1):+.2f}/{f(h2):+.2f}"+(f" | S {len(S)} {np.mean(S):+.2f}" if S else ''),flush=True)
    return P
if __name__=='__main__':
    run('BASE site: stop 20/10 3ATR long sma50')
    for q in (0.7,0.8,0.9): run(f'squeeze ATR10/ATR60<{q}',squeeze=q)
    run('close-confirm 20, next open')
    for vc in (1.2,1.5,2.0): run(f'close-confirm + volume>{vc}x',mode='close',vol_conf=vc)
    run('close-confirm (mode)',mode='close')
    run('regime BTC>SMA200',reg='sma200')
    run('+ shorts when BTC<SMA200',reg='sma200',shorts=True)
    run('+ shorts when BTC sma50 down',shorts=True)
    run('no regime filter',reg='none')
