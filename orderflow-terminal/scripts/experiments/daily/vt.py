import warnings; warnings.filterwarnings('ignore')
from dbt import *
D=load(); btc=D['BTC']; bs=btc.c.rolling(50).mean()
def btc_align(t,d):
    if t not in btc.index: return True
    i=btc.index.get_loc(t)
    if i<55: return True
    dn=btc.c.iloc[i]<bs.iloc[i] and bs.iloc[i]<bs.iloc[i-5]
    return d>0 and not dn
def site(trail=('donchian',10), n_in=20, stop=3.0):
    ps=[]; T=[]
    for s,df in D.items():
        tr,p=simulate(df,donchian(df,n_in=n_in,stop_atr=stop,long_only=True,btc_ok=btc_align),s,10000,trail); ps.append(p.rename(s)); T+=tr
    return pd.concat(ps,axis=1,sort=True).fillna(0), T
def rep(name,P,T=None):
    P=P[P.index>='2021-06-01']; m=metrics(P); h1=P[P.index<'2023-06-01']; h2=P[P.index>='2023-06-01']
    f=lambda x:x.mean()/x.std()*math.sqrt(365)
    print(f"{name:42s} SR {m['sr']:.2f} sumR {P.sum():+6.0f} maxDD {m['mdd']:5.0f}R ret/DD {P.sum()/m['mdd']:.1f} | halves {f(h1):+.2f}/{f(h2):+.2f}"+(f" trades {len(T)} avgR {np.mean([t.r for t in T]):+.2f}" if T else ''))
X,T=site(); P=X.sum(axis=1); rep('SITE 20/10 3ATR long BTC (current)',P,T)
for w in (20,30,40):
    X2,T2=site(('mid',w)); rep(f'  exit: midline {w}d instead of 10d low',X2.sum(axis=1),T2)
# portfolio vol targeting (Moreira-Muir): scale by target/realized vol of past 30/60 days of the portfolio R series
for win in (30,60):
    v=P.rolling(win).std().shift(1); k=(P.std()/v).clip(upper=2).fillna(1)
    rep(f'  vol-managed portfolio (win {win}, cap 2x)',P*k)
    k=(P.std()/v).clip(upper=1).fillna(1); rep(f'  vol-managed portfolio (win {win}, cap 1x)',P*k)
# concurrent-risk cap: count of open positions -> scale each day's pnl by min(1, cap/open)
open_=(X!=0).sum(axis=1)
for cap in (4,6,8):
    rep(f'  cap concurrent risk at {cap}R',P*(cap/open_.shift(0).clip(lower=cap)))
