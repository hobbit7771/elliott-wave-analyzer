# Scalping "orchestra": ridge + logistic + LightGBM (+ ensemble) on order-flow features, day-by-day walk-forward.
import sys, math, numpy as np, pandas as pd, lightgbm as lgb, warnings; warnings.filterwarnings('ignore')
from sklearn.linear_model import Ridge, LogisticRegression
import os
X=pd.read_pickle(os.environ.get('FEAT','feat.pkl')); H=int(sys.argv[1]); TRAIN_DAYS=int(sys.argv[2]) if len(sys.argv)>2 else 10
FEAT=[c for c in X.columns if not (c[0] in 'ye' and c[1:].isdigit()) and c not in ('px','rvraw','day','sym')]
X=X.dropna(subset=[f'y{H}',f'e{H}','rv900']).copy()
X['tgt']=(X[f'y{H}']/(X.rvraw*math.sqrt(H))).clip(-5,5)
X['symc']=X.sym.map({'BTCUSDT':0,'ETHUSDT':1,'SOLUSDT':2})
FEAT=FEAT+['symc']
days=sorted(X.day.unique()); res=[]
for k in range(TRAIN_DAYS,len(days)):
    trd=days[k-TRAIN_DAYS:k]; ted=days[k]
    tr=X[X.day.isin(trd)]; te=X[X.day==ted]
    tr=tr[tr.index < (pd.Timestamp(ted).value//10**9) - H]    # purge rows whose target reaches the test day
    mu=tr[FEAT].median(); sd=tr[FEAT].std().replace(0,1)
    A=((tr[FEAT]-mu)/sd).clip(-6,6).fillna(0).values; Bm=((te[FEAT]-mu)/sd).clip(-6,6).fillna(0).values
    y=tr.tgt.values; o=te[['sym','day','px','rvraw',f'y{H}',f'e{H}','tgt']].copy()
    rd=Ridge(alpha=10).fit(A,y); o['ridge']=rd.predict(Bm); s_r=rd.predict(A).std()
    lg=LogisticRegression(C=0.1,max_iter=300).fit(A[::3],(y[::3]>0).astype(int)); o['logit']=lg.decision_function(Bm); s_l=lg.decision_function(A[::3]).std()
    gb=lgb.LGBMRegressor(n_estimators=300,learning_rate=0.05,num_leaves=31,min_child_samples=2000,subsample=0.5,subsample_freq=1,colsample_bytree=0.7,reg_lambda=10,verbose=-1,random_state=k)
    gb.fit(tr[FEAT].values,y); o['lgbm']=gb.predict(te[FEAT].values); s_g=gb.predict(tr[FEAT].values[::5]).std()
    o['ens']=(o.ridge/s_r+o.logit/s_l+o.lgbm/s_g)/3
    res.append(o)
    if k==TRAIN_DAYS: imp=pd.Series(gb.booster_.feature_importance('gain'),index=FEAT).sort_values(ascending=False); print('LGBM top features:',', '.join(f'{a}' for a in imp.index[:12]))
R=pd.concat(res); R.to_pickle(os.environ.get('PRED',f'smpred_{H}.pkl'))
print(f'==== horizon {H}s, test days {R.day.nunique()} ({R.day.min()}..{R.day.max()}), rows {len(R)}')
for m in ('ridge','logit','lgbm','ens'):
    ics=R.groupby(['day','sym']).apply(lambda g:g[m].corr(g.tgt,method='spearman'))
    print(f'{m:6s} IC {ics.mean():+.4f} (days positive {(ics>0).mean()*100:.0f}%) | ', end='')
    for sym,g in R.groupby('sym'):
        print(f'{sym[:3]} {g[m].corr(g.tgt,method="spearman"):+.3f} ', end='')
    print()
# trading: signed by ensemble, only the strongest signals; non-overlapping trades per symbol; returns in basis points
def trade(m,q,gap):
    out=[]
    for (sym,day),g in R.groupby(['sym','day']):
        thr=np.nanquantile(np.abs(R[m]),q)
        last=-10**12
        for t,row in g[np.abs(g[m])>=thr].iterrows():
            if t<last+gap: continue
            out.append((sym,day,np.sign(row[m])*row[f'e{H}']*1e4)); last=t+H+1
    return pd.DataFrame(out,columns=['sym','day','bps'])
print('trades on |ensemble| in the top x% (enter 1 s after the signal, exit after the horizon), gross bps per trade:')
for q in (0.9,0.99,0.999):
    T=trade('ens',q,0)
    if not len(T): continue
    dd=T.groupby('day').bps.sum()
    print(f'  top {100*(1-q):.1f}%: trades {len(T):6d} ({len(T)/R.day.nunique():.0f}/day) gross {T.bps.mean():+6.2f} bps  win {np.mean(T.bps>0)*100:.1f}% | net taker 10bps {T.bps.mean()-10:+6.2f}, maker 4bps {T.bps.mean()-4:+6.2f}, maker 0 {T.bps.mean():+6.2f} | by sym '+' '.join(f"{s[:3]} {g.bps.mean():+.2f}" for s,g in T.groupby('sym'))+f' | days gross>0 {(dd>0).mean()*100:.0f}%')
