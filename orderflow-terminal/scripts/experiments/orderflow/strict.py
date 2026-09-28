import sys, numpy as np, pandas as pd
for H in (300,900,1800,3600):
    R=pd.read_pickle(f'smpred_{H}.pkl'); days=sorted(R.day.unique())
    for q in (0.99,0.999):
        out=[]
        for k,d in enumerate(days):
            if k<5: continue
            past=R[R.day.isin(days[:k])]; thr=np.quantile(np.abs(past.ens),q)   # threshold from past days only
            for sym,g in R[R.day==d].groupby('sym'):
                last=-10**12
                for t,row in g[np.abs(g.ens)>=thr].iterrows():
                    if t<last: continue
                    out.append((d,np.sign(row.ens)*row[f'e{H}']*1e4)); last=t+H+1
        T=pd.DataFrame(out,columns=['day','bps'])
        if len(T)<10: continue
        daily=T.groupby('day').bps.sum(); rng=np.random.default_rng(0)
        boot=[rng.choice(T.bps.values,len(T)).mean() for _ in range(3000)]
        print(f"H {H:4d}s top {100*(1-q):.1f}% (causal thr): trades {len(T):5d} gross {T.bps.mean():+6.2f} bps  95% CI [{np.percentile(boot,2.5):+.2f}, {np.percentile(boot,97.5):+.2f}]  P(gross<=4bps maker) {np.mean(np.array(boot)<=4):.2f}  win {np.mean(T.bps>0)*100:.0f}%")
