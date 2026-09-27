import warnings; warnings.filterwarnings('ignore')
import io,contextlib
src=open('ftr.py').read().split("run('BASE, funding estimate")[0]; exec(src)
for thr in (0.0003,0.0004,0.0005,0.0007):
    kept=[];skip=[]
    for s,df in D.items():
        sig=gen(df); fa=fu[s].fillna(FUND_LONG).values
        tr,_=simulate(df,sig,s,10000,('donchian',10),fund=fa)
        for t in tr:
            i=df.index.get_loc(t.t0)-1  # signal day = day before the fill (valid=1)
            (skip if F7.iloc[:,list(F7.columns).index(s)].iloc[i]>thr else kept).append((t.t0.year,t.r))
    sk=np.array([r for _,r in skip]); kp=np.array([r for _,r in kept])
    bs=[np.random.default_rng(k).choice(sk,len(sk)).mean() for k in range(2000)]
    print(f"thr {thr*100:.2f}%/day: skipped {len(sk)} avgR {sk.mean():+.2f} (boot 90% {np.percentile(bs,5):+.2f}..{np.percentile(bs,95):+.2f}) win {np.mean(sk>0)*100:.0f}% | kept {len(kp)} avgR {kp.mean():+.2f} | skipped by year", dict(pd.Series([y for y,_ in skip]).value_counts().sort_index()))
