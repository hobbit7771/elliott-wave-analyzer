import warnings; warnings.filterwarnings('ignore')
import io,contextlib,sys,os
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('ftr.py').read().split("run('BASE, funding estimate")[0])
filt=lambda t,s:not (F7.at[t,s]>0.0005)
ps=[]
for s,df in D.items():
    sig=[x for x in gen(df) if filt(df.index[x['i']],s)]
    tr,p=simulate(df,sig,s,10000,('donchian',10),fund=fu[s].fillna(FUND_LONG).values); ps.append(p)
TR=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1)          # trend model, R per day
os.chdir('carry'); sys.argv=['x']
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('carry.py').read().split("if __name__")[0])
CA=run(entry=0.10,exit=0.03,K=10,verbose=False)                   # carry, return on capital per day
idx=TR.index.intersection(CA.index); idx=idx[idx>='2021-06-01']
def rep(name,r):
    r=r.loc[idx]; eq=(1+r).cumprod(); yrs=len(r)/365; cagr=eq.iloc[-1]**(1/yrs)-1; vol=r.std()*math.sqrt(365); dd=(1-eq/eq.cummax()).max()
    by=r.groupby(r.index.year).apply(lambda x:(1+x).prod()-1)
    print(f"{name:46s} CAGR {cagr*100:+6.1f}% vol {vol*100:5.1f}% SR {r.mean()*365/vol:5.2f} maxDD {dd*100:5.1f}% | "+' '.join(f"{y}:{v*100:+.0f}%" for y,v in by.items()))
print('corr(trend, carry) daily:', round(np.corrcoef(TR.loc[idx],CA.loc[idx])[0,1],3), ' monthly:', round(TR.loc[idx].resample('ME').sum().corr(CA.loc[idx].resample('ME').sum()),3))
for risk in (0.0025,0.005,0.01):
    rep(f'trend alone, risk {risk*100:.2f}%/trade',TR*risk)
rep('carry alone (rotate >10%, K=10)',CA)
for risk in (0.0025,0.005,0.01):
    rep(f'trend {risk*100:.2f}%/trade + carry on the same capital',TR*risk+CA)
