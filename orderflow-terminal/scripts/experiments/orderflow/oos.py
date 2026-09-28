# Pre-registered out-of-sample check: H=3600 s, ensemble, top 0.1 % by a threshold from past days only.
import sys, numpy as np, pandas as pd
R=pd.read_pickle(sys.argv[1]); H=3600; q=0.999; days=sorted(R.day.unique()); out=[]
for k,d in enumerate(days):
    if k<5: continue
    thr=np.quantile(np.abs(R[R.day.isin(days[:k])].ens),q)
    for sym,g in R[R.day==d].groupby('sym'):
        last=-10**12
        for t,row in g[np.abs(g.ens)>=thr].iterrows():
            if t<last: continue
            out.append((d,sym,np.sign(row.ens)*row[f'e{H}']*1e4)); last=t+H+1
T=pd.DataFrame(out,columns=['day','sym','bps']); rng=np.random.default_rng(0)
boot=[rng.choice(T.bps.values,len(T)).mean() for _ in range(5000)]
print(f"OOS {days[0]}..{days[-1]}: trades {len(T)} gross {T.bps.mean():+.2f} bps 95% CI [{np.percentile(boot,2.5):+.2f}, {np.percentile(boot,97.5):+.2f}] win {np.mean(T.bps>0)*100:.0f}% | net maker 4bps {T.bps.mean()-4:+.2f}, taker 10bps {T.bps.mean()-10:+.2f}")
print(' by symbol:', ' '.join(f"{s[:3]} n={len(g)} {g.bps.mean():+.2f}" for s,g in T.groupby('sym')))
T['m']=T.day.str[:7]; print(' by month:', ' '.join(f"{m} n={len(g)} {g.bps.mean():+.1f}" for m,g in T.groupby('m')))
