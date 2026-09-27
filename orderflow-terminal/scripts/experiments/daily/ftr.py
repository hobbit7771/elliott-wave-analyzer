import warnings; warnings.filterwarnings('ignore')
from dbt import *
from r2 import gen, D
fu={}; 
for s,df in D.items():
    dn=(df.index-pd.Timestamp('1970-01-01')).days; f=dict(json.load(open(f'fund/{s}.json')))
    fu[s]=pd.Series([f.get(int(d),np.nan)/1e6 for d in dn],index=df.index)
F1=pd.DataFrame(fu); F7=F1.rolling(7).mean(); F30=F1.rolling(30,min_periods=10).mean()
def rank_ok(F,t,s,maxpct):
    row=F.loc[t].dropna()
    if s not in row or len(row)<8: return True
    return row.rank(pct=True)[s]<=maxpct
def run(name, real=True, filt=None, **kw):
    ps=[]; T=[]
    for s,df in D.items():
        sig=gen(df,**kw)
        if filt: sig=[x for x in sig if filt(df.index[x['i']],s)]
        fa=fu[s].fillna(FUND_LONG).values if real else None
        tr,p=simulate(df,sig,s,10000,('donchian',10),fund=fa); ps.append(p.rename(s)); T+=tr
    P=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1); P=P[P.index>='2021-06-01']
    m=metrics(P); f=lambda x:x.mean()/x.std()*math.sqrt(365)
    h1=P[P.index<'2023-06-01']; h2=P[P.index>='2023-06-01']
    print(f"{name:46s} n {len(T):4d} avgR {np.mean([t.r for t in T]):+.2f} SR {m['sr']:.2f} sumR {P.sum():+5.0f} DD {m['mdd']:4.0f} halves {f(h1):+.2f}/{f(h2):+.2f}",flush=True)
run('BASE, funding estimate 0.03%/day',real=False)
run('BASE, real Bybit funding')
for q in (0.5,0.67,0.8):
    run(f'skip if coin funding7 rank > {q:.0%}',filt=lambda t,s,q=q:rank_ok(F7,t,s,q))
    run(f'skip if coin funding30 rank > {q:.0%}',filt=lambda t,s,q=q:rank_ok(F30,t,s,q))
for thr in (0.0005,0.001,0.0015):
    run(f'skip if funding7 > {thr*100:.2f}%/day',filt=lambda t,s,thr=thr:not (F7.at[t,s]>thr))
