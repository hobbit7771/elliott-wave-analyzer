# Cross-exchange funding arbitrage (Binance vs Bybit perpetuals): short the perp with the higher funding, long the other.
import json, math, numpy as np, pandas as pd, os
os.chdir('carry')
src=open('carry.py').read().split("if __name__")[0]; g={}; exec(src,g)
FB=g['F']; PB=g['PP']                                       # Binance daily funding sums and perp closes
os.chdir('..')
common=[s for s in FB.columns if os.path.exists(f'fund/{s}.json') and os.path.exists(f'daily/{s}USDT.json')]
FY={}; PY={}
for s in common:
    d=dict(json.load(open(f'fund/{s}.json'))); FY[s]=pd.Series({pd.Timestamp(k*86400000,unit='ms'):v/1e6 for k,v in d.items()})
    a=np.array(json.load(open(f'daily/{s}USDT.json'))); PY[s]=pd.Series(a[:,4],index=pd.to_datetime(a[:,0],unit='ms'))
FY=pd.DataFrame(FY).reindex(FB.index); PY=pd.DataFrame(PY).reindex(FB.index)
FB=FB[common]; PB=PB[common]
print('coins',common)
diff=FB-FY                                                  # >0: Binance funding higher -> short Binance, long Bybit
print('mean |daily funding diff| (bp/day):', (diff.abs().mean()*1e4).round(2).to_dict())
def run(entry=0.10, exit=0.03, K=6, lb=7, cost_side=0.0011, name=''):
    ann=diff.rolling(lb,min_periods=lb).mean()*365
    ok=FB.notna()&FY.notna()&PB.notna()&PY.notna()
    W=pd.DataFrame(0.0,index=FB.index,columns=common); held={}
    for t in FB.index:
        a=ann.loc[t]; o=ok.loc[t]
        held={s:d for s,d in held.items() if o[s] and a[s]*d>exit}
        for s in a[o&(a.abs()>entry)].abs().sort_values(ascending=False).index:
            if len(held)>=K: break
            if s not in held: held[s]=1 if a[s]>0 else -1
        for s,d in held.items(): W.at[t,s]=d/K
    Wd=W.shift(1).fillna(0)
    rb=PB.pct_change(); ry=PY.pct_change()
    # d=+1: short Binance perp (receive FB, pay -rb), long Bybit perp (pay FY, get ry)
    pnl=(Wd*((FB-FY).fillna(0)+(ry-rb).fillna(0))).sum(axis=1)-W.diff().abs().fillna(W.abs()).sum(axis=1).shift(1).fillna(0)*cost_side*2
    r=pnl/0.5*0.25    # capital: two perp legs at ~4x... use margin 25 % of each leg's notional -> capital 0.5 N; scale 0.25 = 50 % margin buffer
    r=pnl/1.0          # conservative: capital = one full notional (both legs margined at 50 %)
    r=r[r.index>='2021-01-01']; eq=(1+r).cumprod(); yrs=len(r)/365; cagr=eq.iloc[-1]**(1/yrs)-1; vol=r.std()*math.sqrt(365); dd=(1-eq/eq.cummax()).max()
    by=r.groupby(r.index.year).apply(lambda x:(1+x).prod()-1)
    print(f"{name:40s} CAGR {cagr*100:+5.1f}% vol {vol*100:4.1f}% SR {r.mean()*365/vol if vol>0 else 0:5.2f} maxDD {dd*100:4.1f}% expo {Wd.abs().sum(axis=1).mean():.2f} | "+' '.join(f"{y}:{v*100:+.1f}%" for y,v in by.items()))
    return r
for e,x in ((0.05,0.01),(0.10,0.03),(0.20,0.05)):
    for K in (3,6):
        run(e,x,K,name=f'xarb enter>{e:.0%} exit<{x:.0%} K={K}')
