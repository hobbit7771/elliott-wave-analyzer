import warnings; warnings.filterwarnings('ignore')
from dbt import *
import itertools, pickle, sys
D=load(); btc=D['BTC']; bs=btc.c.rolling(50).mean()
def btc_align(t,d):
    if t not in btc.index: return True
    i=btc.index.get_loc(t)
    if i<55: return True
    up=btc.c.iloc[i]>bs.iloc[i] and bs.iloc[i]>bs.iloc[i-5]; dn=btc.c.iloc[i]<bs.iloc[i] and bs.iloc[i]<bs.iloc[i-5]
    return (d>0 and not dn) or (d<0 and not up)
variants=[]
for n_in,n_out,st,lo,bf,er in itertools.product([20,40,55,100],[10,20],[2.0,3.0],[True,False],[False,True],[None,0.3]):
    if n_out>=n_in: continue
    variants.append(dict(n_in=n_in,n_out=n_out,stop_atr=st,long_only=lo,btc=bf,er_min=er))
cols={}; trades={}
for k,v in enumerate(variants):
    ps=[]; T=[]
    for s,df in D.items():
        sig=donchian(df,n_in=v['n_in'],stop_atr=v['stop_atr'],long_only=v['long_only'],btc_ok=btc_align if v['btc'] else None,er_min=v['er_min'])
        tr,p=simulate(df,sig,s,10000,('donchian',v['n_out'])); ps.append(p.rename(s)); T+=[(t.t0,t.t1,t.r,s,t.d) for t in tr]
    cols[k]=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1); trades[k]=T
    print(k, v, 'SR', round(metrics(cols[k])['sr'],2), 'trades', len(T), flush=True)
M=pd.DataFrame(cols).fillna(0)
pickle.dump((variants,M,trades),open('wf_don.pkl','wb'))
