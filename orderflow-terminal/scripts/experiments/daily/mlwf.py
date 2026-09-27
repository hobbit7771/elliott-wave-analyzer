import warnings; warnings.filterwarnings('ignore')
import sys, math, numpy as np, pandas as pd, lightgbm as lgb
from sklearn.linear_model import Ridge, LogisticRegression
from sklearn.ensemble import ExtraTreesRegressor
P=pd.read_pickle('panel.pkl')
H=int(sys.argv[1]) if len(sys.argv)>1 else 5
FEAT=[c for c in P.columns if c not in ('sym','age','y1','y5','y10','n_coins')]
P=P.dropna(subset=[f'y{H}','vol30']).copy()
P['tgt']=P[f'y{H}']/(P.vol30*math.sqrt(H))           # vol-scaled forward return
P['tgt_xs']=P.tgt-P.groupby(level=0).tgt.transform('mean')
days=P.index.unique().sort_values(); start=pd.Timestamp('2022-06-01')
test_starts=[d for d in pd.date_range(start,days[-1],freq='30D')]
preds=[]
for k,t0 in enumerate(test_starts):
    t1=test_starts[k+1] if k+1<len(test_starts) else days[-1]+pd.Timedelta(days=1)
    tr=P[P.index<=t0-pd.Timedelta(days=H+1)]; te=P[(P.index>=t0)&(P.index<t1)]
    if not len(te): continue
    mu=tr[FEAT].median(); sd=tr[FEAT].std().replace(0,1)
    Xtr=((tr[FEAT]-mu)/sd).clip(-5,5).fillna(0).values; Xte=((te[FEAT]-mu)/sd).clip(-5,5).fillna(0).values
    y=tr.tgt.clip(-4,4).values
    out=pd.DataFrame(index=te.index); out['sym']=te.sym.values; out['y']=te[f'y{H}'].values; out['tgt']=te.tgt.values; out['vol30']=te.vol30.values
    out['ridge']=Ridge(alpha=100).fit(Xtr,y).predict(Xte)
    out['logit']=LogisticRegression(C=0.05,max_iter=500).fit(Xtr,(y>0).astype(int)).predict_proba(Xte)[:,1]-0.5
    g=lgb.LGBMRegressor(n_estimators=300,learning_rate=0.02,num_leaves=8,min_child_samples=200,subsample=0.7,subsample_freq=1,colsample_bytree=0.7,reg_lambda=5,verbose=-1,random_state=k)
    out['lgbm']=g.fit(Xtr,y).predict(Xte)
    out['etr']=ExtraTreesRegressor(n_estimators=150,min_samples_leaf=200,max_features=0.5,n_jobs=4,random_state=k).fit(Xtr,y).predict(Xte)
    gx=lgb.LGBMRegressor(n_estimators=300,learning_rate=0.02,num_leaves=8,min_child_samples=200,subsample=0.7,subsample_freq=1,colsample_bytree=0.7,reg_lambda=5,verbose=-1,random_state=k)
    out['lgbm_xs']=gx.fit(Xtr,tr.tgt_xs.clip(-4,4).values).predict(Xte)
    preds.append(out)
    if k%6==0: print('trained up to',t0.date(),'train rows',len(tr),flush=True)
R=pd.concat(preds)
M=['ridge','logit','lgbm','etr','lgbm_xs']
for m in M: R[m+'_z']=R.groupby(level=0)[m].rank(pct=True)
R['ens']=R[[m+'_z' for m in M[:4]]].mean(axis=1)   # rank-average ensemble (cross-sectional)
# time-series ensemble score: average of standardized raw predictions (keeps the market direction)
for m in M[:4]: R[m+'_s']=(R[m]-R[m].expanding().mean())/R[m].expanding().std()
R['ens_ts']=R[[m+'_s' for m in M[:4]]].mean(axis=1)
R.to_pickle(f'mlpred_h{H}.pkl')
def ic(col,target='y',step=H):
    s=R.groupby(level=0).apply(lambda g:g[col].rank().corr(g[target].rank()) if len(g)>=8 else np.nan).dropna().iloc[::step]
    return s.mean(), s.mean()/s.std()*math.sqrt(len(s))
print(f'--- horizon {H}d: cross-sectional IC (Spearman), non-overlapping, t-stat ---')
for m in M+['ens']:
    a,t=ic(m); print(f'{m:8s} IC {a:+.4f} t {t:+.2f}')
# time-series predictive: pooled correlation of ens_ts with y, and hit-rate of sign
g=R.iloc[::1]; 
for m in M[:4]+['ens_ts']:
    x=R[m if m=='ens_ts' else m]; c=np.corrcoef(x.rank(),R.y.rank())[0,1]
    print(f'TS pooled rank-corr {m:7s} {c:+.4f}  hit {( (x>x.median())==(R.y>0) ).mean()*100:.1f}%')
