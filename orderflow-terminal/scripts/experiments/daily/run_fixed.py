from dbt import *
D=load(); btc=D['BTC']; bs=btc.c.rolling(50).mean()
def btc_align(t,d):
    if t not in btc.index: return True
    i=btc.index.get_loc(t)
    if i<55: return True
    up=btc.c.iloc[i]>bs.iloc[i] and bs.iloc[i]>bs.iloc[i-5]; dn=btc.c.iloc[i]<bs.iloc[i] and bs.iloc[i]<bs.iloc[i-5]
    return (d>0 and not dn) or (d<0 and not up)
def run(name, gen, hold, trail=None, **kw):
    allp=[]; T=[]
    for s,df in D.items():
        tr,p=simulate(df, gen(df,**kw), s, hold, trail); T+=tr; allp.append(p.rename(s))
    P=pd.concat(allp,axis=1).fillna(0).sum(axis=1)
    m=metrics(P); rs=np.array([t.r for t in T])
    yrs=P.groupby(P.index.year).sum()
    L=[t.r for t in T if t.d>0]; S=[t.r for t in T if t.d<0]
    print(f"{name:34s} trades {len(T):4d} avgR {rs.mean():+.2f} win {np.mean(rs>0)*100:3.0f}% | L {len(L)} {np.mean(L) if L else 0:+.2f} S {len(S)} {np.mean(S) if S else 0:+.2f} | SR {m['sr']:.2f} sumR {P.sum():+.0f} maxDD {m['mdd']:.0f}R | by year: "+' '.join(f"{y}:{v:+.0f}" for y,v in yrs.items()))
    return P,T
run('Holy Grail D1 (book, hold 10)', holy_grail, 10)
run('Holy Grail D1 long only', holy_grail, 10, long_only=True)
run('Holy Grail D1 + BTC trend align', holy_grail, 10, btc_ok=btc_align)
run('Turtle 55/20 (Donchian)', donchian, 1000, ('donchian',20), n_in=55)
run('Turtle 20/10', donchian, 1000, ('donchian',10), n_in=20)
run('Turtle 55/20 long only', donchian, 1000, ('donchian',20), n_in=55, long_only=True)
run('Turtle 55/20 + BTC align', donchian, 1000, ('donchian',20), n_in=55, btc_ok=btc_align)
run('Donchian 55 + chandelier 3ATR', donchian, 1000, ('chandelier',3.0), n_in=55)
