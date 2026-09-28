# Trend model (20/10 breakout, 3 ATR, long-only, BTC filter, funding filter) on 17 vs 35 coins (Binance perps 2020-2026).
import warnings; warnings.filterwarnings('ignore')
import math, glob, numpy as np, pandas as pd, io, contextlib
from dbt import simulate, metrics, atr, FUND_LONG
import r2
def ts(x): x=np.asarray(x,dtype='int64'); return np.where(x>1e14,x//1000,x)
def day(x): return pd.to_datetime(ts(x)//86400000*86400000,unit='ms')
S=sorted({f.split('/')[-1].split('_')[0] for f in glob.glob('carry/raw/*_perp.csv')})
D={}; FU={}
for s in S:
    k=pd.read_csv(f'carry/raw/{s}_perp.csv',header=None); df=pd.DataFrame({'o':k[1].values,'h':k[2].values,'l':k[3].values,'c':k[4].values,'v':k[5].values},index=day(k[0])).groupby(level=0).last()
    D[s]=df[df.index>='2020-01-01']
    f=pd.read_csv(f'carry/raw/{s}_fund.csv',header=None); FU[s]=f.groupby(day(f[0]))[2].sum()
r2.D=D; r2.btc=D['BTC']; r2.bs50=D['BTC'].c.rolling(50).mean(); r2.bs200=D['BTC'].c.rolling(200).mean()
F1=pd.DataFrame(FU); F7=F1.rolling(7).mean()
OLD=['BTC','ETH','SOL','XRP','DOGE','BNB','ADA','LINK','AVAX','SUI','UNI','INJ']   # the site's watchlist today (12)
def run(coins,name):
    ps=[]; n=0
    for s in coins:
        df=D[s]; f=F7[s].reindex(df.index) if s in F7 else None
        sig=[x for x in r2.gen(df) if not (f is not None and f.iloc[x['i']]>0.0005)]
        fa=F1[s].reindex(df.index).fillna(FUND_LONG).values if s in F1 else None
        tr,p=simulate(df,sig,s,10000,('donchian',10),fund=fa); ps.append(p); n+=len(tr)
    P=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1); P=P[P.index>='2021-06-01']
    Pn=P/len(coins)*12   # same total risk budget as 12 coins: risk per trade scaled by 12/N
    m=metrics(Pn); s_=lambda x:x.mean()/x.std()*math.sqrt(365); h1=Pn[Pn.index<'2024-01-01']; h2=Pn[Pn.index>='2024-01-01']
    print(f"{name:34s} coins {len(coins):2d} trades {n:4d} | per-12-coin-budget: SR {m['sr']:.2f} sumR {Pn.sum():+6.0f} maxDD {m['mdd']:5.0f}R ret/DD {Pn.sum()/m['mdd']:.1f} halves {s_(h1):+.2f}/{s_(h2):+.2f}")
    return Pn
a=run([c for c in OLD if c in D],'site watchlist (12 coins)')
b=run(S,'all 35 Binance coins')
print('corr of the two daily series', round(a.corr(b),2))

# causal universe: at the start of each month, the top-N coins by 30-day dollar volume (only those are traded that month)
DV=pd.DataFrame({s:(D[s].c*D[s].v).rolling(30,min_periods=20).mean() for s in S})
def run_dyn(N,name,extra=None):
    months=pd.date_range('2020-03-01','2026-10-01',freq='MS'); member={}
    for m in months:
        prev=DV[DV.index<m]
        if not len(prev): continue
        top=prev.iloc[-1].dropna().sort_values(ascending=False).index[:N]
        for s in top: member.setdefault(s,[]).append(m)
    ps=[]; n=0
    for s,ms in member.items():
        df=D[s]; f=F7[s].reindex(df.index) if s in F7 else None
        ok=set((m.year,m.month) for m in ms)
        sig=[x for x in r2.gen(df) if (df.index[x['i']].year,df.index[x['i']].month) in ok and not (f is not None and f.iloc[x['i']]>0.0005)]
        fa=F1[s].reindex(df.index).fillna(FUND_LONG).values if s in F1 else None
        tr,p=simulate(df,sig,s,10000,('donchian',10),fund=fa); ps.append(p); n+=len(tr)
    P=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1); P=P[P.index>='2021-06-01']; Pn=P/N*12
    m=metrics(Pn); s_=lambda x:x.mean()/x.std()*math.sqrt(365); h1=Pn[Pn.index<'2024-01-01']; h2=Pn[Pn.index>='2024-01-01']
    print(f"{name:34s} coins/month {N:2d} trades {n:4d} | per-12-coin-budget: SR {m['sr']:.2f} sumR {Pn.sum():+6.0f} maxDD {m['mdd']:5.0f}R ret/DD {Pn.sum()/m['mdd']:.1f} halves {s_(h1):+.2f}/{s_(h2):+.2f}")
for N in (8,12,20,35): run_dyn(N,f'monthly top-{N} by volume (causal)')
