# Daily panel dataset: features known at the close of day i, targets from the open of i+1.
import warnings; warnings.filterwarnings('ignore')
from dbt import *
D=load()
def coin(s):
    df=D[s].copy(); dn=(df.index-pd.Timestamp('1970-01-01')).days
    fu=dict(json.load(open(f'fund/{s}.json'))); oi=dict(json.load(open(f'oi/{s}.json')))
    lc=np.log(df.c); r=lc.diff(); F=pd.DataFrame(index=df.index)
    v30=r.rolling(30).std(); v7=r.rolling(7).std(); F['vol30']=v30; F['volr']=v7/v30
    for n in (1,3,7,14,30,60,120): F[f'r{n}']=(lc-lc.shift(n))/(v30*math.sqrt(n))
    for n in (20,55):
        hi=df.h.rolling(n).max(); lo=df.l.rolling(n).min()
        F[f'pos{n}']=(df.c-lo)/(hi-lo); F[f'dhi{n}']=np.log(df.c/hi)/v30
    A=atr(df,20); F['rng']=(df.h-df.l)/A; F['body']=(df.c-df.o)/A
    F['clv']=((df.c-df.l)-(df.h-df.c))/(df.h-df.l).replace(0,np.nan)
    F['vr']=np.log(df.v/df.v.rolling(20).mean()); F['vr5']=np.log(df.v.rolling(5).mean()/df.v.rolling(60).mean())
    f1=pd.Series([fu.get(int(d),np.nan)/1e6 for d in dn],index=df.index)
    F['f1']=f1*1e4; F['f7']=f1.rolling(7).mean()*1e4; F['f30']=f1.rolling(30,min_periods=10).mean()*1e4; F['fdev']=F.f7-F.f30
    o=pd.Series([oi.get(int(d),np.nan) for d in dn],index=df.index)  # snapshot at the start of day: known at close
    F['oi1']=np.log(o/o.shift(1)); F['oi7']=np.log(o/o.shift(7)); F['oi30']=np.log(o/o.shift(30))
    F['oi_px7']=F.oi7*np.sign(F.r7)
    F['sma50']=np.log(df.c/df.c.rolling(50).mean())/v30; F['sma200']=np.log(df.c/df.c.rolling(200).mean())/v30
    for h in (1,5,10): F[f'y{h}']=np.log(df.o.shift(-1-h)/df.o.shift(-1))
    F['sym']=s; F['age']=np.arange(len(df))
    return F
P=pd.concat([coin(s) for s in D]); P.index.name='t'
b=P[P.sym=='BTC'][['r1','r7','r30','sma50','vol30']].add_prefix('btc_')
P=P.join(b,on='t')
for c in ('r7','r30','f7','f30','oi7','vr5','sma50'):
    P[f'xs_{c}']=P.groupby(level=0)[c].rank(pct=True)
P['n_coins']=P.groupby(level=0).sym.transform('count')
P=P[(P.age>=130)&(P.index>='2021-06-01')]
P.to_pickle('panel.pkl'); print(P.shape, P.index.min().date(), P.index.max().date()); print(P.isna().mean().sort_values().tail(6))
