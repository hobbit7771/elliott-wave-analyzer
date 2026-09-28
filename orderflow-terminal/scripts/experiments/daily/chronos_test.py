# Zero-shot Chronos-Bolt forecasts of daily log prices: direction and magnitude of the next 5 days vs reality.
import warnings; warnings.filterwarnings('ignore')
import math, numpy as np, pandas as pd, torch
from chronos import BaseChronosPipeline
from dbt import load
D=load(); H=5; CTX=512
pipe=BaseChronosPipeline.from_pretrained('amazon/chronos-bolt-small', device_map='cpu', torch_dtype=torch.float32)
dates=pd.date_range('2023-01-02','2026-09-10',freq='7D')
rows=[]
for s,df in D.items():
    lc=np.log(df.c)
    batch=[]; meta=[]
    for d in dates:
        if d not in df.index: continue
        i=df.index.get_loc(d)
        if i<CTX or i+H>=len(df): continue
        batch.append(torch.tensor(lc.values[i-CTX+1:i+1],dtype=torch.float32)); meta.append((d,lc.values[i],lc.values[i+H],lc.values[i]-lc.values[i-20]))
    if not batch: continue
    q=pipe.predict(batch, prediction_length=H)          # [n, 9 quantiles, H]
    med=q[:,4,-1].numpy()
    for (d,now,fut,mom),m in zip(meta,med): rows.append((s,d,m-now,fut-now,mom))
R=pd.DataFrame(rows,columns=['sym','day','pred','real','mom20'])
def rep(name,g):
    hit=np.mean(np.sign(g.pred)==np.sign(g.real)); ic=g.pred.corr(g.real,method='spearman')
    hm=np.mean(np.sign(g.mom20)==np.sign(g.real))
    xs=g.groupby('day').apply(lambda x:x.pred.corr(x.real,method='spearman') if len(x)>=8 else np.nan).dropna()
    # simple trade: long if pred>0, short if pred<0, 5 days, 0.15 % round trip
    pnl=np.sign(g.pred)*g.real-0.0015
    print(f"{name:26s} n={len(g):5d} direction hit {hit*100:5.1f}% (momentum20 {hm*100:5.1f}%, up-days share {np.mean(g.real>0)*100:4.1f}%) | rank IC {ic:+.3f} | cross-sect IC {xs.mean():+.3f} | long/short by sign: {pnl.mean()*1e4:+.1f} bp/trade")
print('forecasts', len(R))
rep('all 2023-2026',R)
rep('2023-2024 (model may have seen)',R[R.day<'2025-01-01'])
rep('2025-2026 (after release: clean)',R[R.day>='2025-01-01'])
