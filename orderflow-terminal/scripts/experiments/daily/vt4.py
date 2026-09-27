import warnings; warnings.filterwarnings('ignore')
import io,contextlib
with contextlib.redirect_stdout(io.StringIO()):
    from vt import *
T.sort(key=lambda t:t.t0)
idx=P.index
realized=pd.Series(0.0,index=idx)
for t in T: realized[t.t1]+=t.r
MTMk={}
def test(win, source, cap=1.0, name=''):
    ser = realized if source=='realized' else P
    v=ser.rolling(win).std(); ref=v.expanding(180).median()
    k=(ref/v).clip(upper=cap)
    tot=[]
    for s,df in D.items():
        tr,p=simulate(df,donchian(df,n_in=20,stop_atr=3.0,long_only=True,btc_ok=btc_align),s,10000,('donchian',10))
        m=pd.Series(1.0,index=df.index)
        for t in tr:
            kk=k.asof(t.t0-pd.Timedelta(days=1)); kk=1.0 if pd.isna(kk) else kk
            m[(m.index>=t.t0)&(m.index<=t.t1)]=kk
        tot.append((p*m).rename(s))
    Q=pd.concat(tot,axis=1,sort=True).fillna(0).sum(axis=1); rep(name,Q); return Q
rep('base',P)
for w in (30,60,90):
    test(w,'realized',name=f'k at entry from realized-R vol {w}d')
    test(w,'mtm',name=f'k at entry from MTM vol {w}d')
