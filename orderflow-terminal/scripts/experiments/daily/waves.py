# ZigZag wave structure + candlestick patterns + context -> reversal model (LightGBM, walk-forward). Daily, 17 coins.
import warnings; warnings.filterwarnings('ignore')
import math, numpy as np, pandas as pd, lightgbm as lgb
from dbt import load, atr
D=load(); P=pd.read_pickle('pat.pkl')
PAT=['bull_engulf','bear_engulf','hammer','shooting_star','doji','inside','morning_star','evening_star']
def waves(df, k=2.0):
    """Causal ZigZag: a pivot is confirmed only when price has moved k*ATR back from the extreme.
    Features at day i use confirmed pivots and the current (unconfirmed) leg up to day i."""
    h,l,c=df.h.values,df.l.values,df.c.values; A=atr(df,14).values; n=len(df)
    piv=[]  # confirmed pivots: (index, price, type +1 high / -1 low)
    dirn=0; ext_i=0; ext_p=c[0]
    F=np.full((n,16),np.nan)
    for i in range(1,n):
        a=A[i] if A[i]>0 else np.nan
        if dirn>=0 and h[i]>ext_p if dirn==1 else False: ext_i,ext_p=i,h[i]
        if dirn==-1 and l[i]<ext_p: ext_i,ext_p=i,l[i]
        if dirn==0:
            if h[i]-l[:i+1].min()>k*a: dirn=1; ext_i,ext_p=i,h[i]
            elif h[:i+1].max()-l[i]>k*a: dirn=-1; ext_i,ext_p=i,l[i]
        elif dirn==1 and ext_p-l[i]>k*a:
            piv.append((ext_i,ext_p,1)); dirn=-1; ext_i,ext_p=i,l[i]
        elif dirn==-1 and h[i]-ext_p>k*a:
            piv.append((ext_i,ext_p,-1)); dirn=1; ext_i,ext_p=i,h[i]
        if len(piv)<4 or not a==a: continue
        p=piv[-4:]; legs=[(p[j+1][1]-p[j][1])/a for j in range(3)]           # last three completed legs (signed, ATR)
        cur=(c[i]-p[-1][1])/a                                                  # current leg from the last pivot
        hh=1 if [q for q in piv if q[2]==1][-1][1] > ([q for q in piv if q[2]==1][-2][1] if len([q for q in piv if q[2]==1])>1 else 0) else -1
        hl=1 if [q for q in piv if q[2]==-1][-1][1] > ([q for q in piv if q[2]==-1][-2][1] if len([q for q in piv if q[2]==-1])>1 else 0) else -1
        # count of consecutive same-structure swings (impulse "wave number" proxy)
        cnt=0
        for j in range(len(piv)-1,1,-1):
            if (piv[j][1]-piv[j-2][1])*(1 if piv[j][2]==1 else 1)*np.sign(piv[-1][1]-piv[-3][1])>0: cnt+=1
            else: break
        F[i]=[dirn, cur, i-p[-1][0], legs[0], legs[1], legs[2],
              abs(cur)/abs(legs[2]) if legs[2] else np.nan,                     # retracement / extension of the last leg
              abs(legs[2])/abs(legs[1]) if legs[1] else np.nan, abs(legs[1])/abs(legs[0]) if legs[0] else np.nan,
              hh, hl, cnt, (c[i]-ext_p)/a, i-ext_i, p[-1][0]-p[-2][0], p[-2][0]-p[-3][0]]
    cols=['dir','cur','cur_days','leg1','leg2','leg3','retr','ext32','ext21','hh','hl','nwave','from_ext','ext_days','dur3','dur2']
    return pd.DataFrame(F,index=df.index,columns=cols)
def ctx(df):
    c=df.c; A=atr(df,14); r=c.diff(); up=r.clip(lower=0).ewm(alpha=1/14).mean(); dn=(-r.clip(upper=0)).ewm(alpha=1/14).mean()
    x=pd.DataFrame(index=df.index)
    x['rsi']=100-100/(1+up/dn); x['sma50']=(c/c.rolling(50).mean()-1)/(A/c); x['sma200']=(c/c.rolling(200).mean()-1)/(A/c)
    x['pos20']=(c-df.l.rolling(20).min())/(df.h.rolling(20).max()-df.l.rolling(20).min()); x['vr']=np.log(df.v/df.v.rolling(20).mean())
    x['atrp']=A/c; x['r5']=(c/c.shift(5)-1)/(A/c)
    return x
rows=[]
for s,df in D.items():
    w=waves(df); x=ctx(df); pp=P[P.sym==s][PAT+['L','S']].astype(float)
    rows.append(pd.concat([w,x],axis=1).join(pp,how='inner').assign(sym=s))
X=pd.concat(rows); X=X[X.index>='2021-06-01'].dropna(subset=['L','S','dir'])
FEAT=[c for c in X.columns if c not in ('L','S','sym')]
print('rows',len(X),'features',len(FEAT))
days=X.index.unique().sort_values(); starts=pd.date_range('2022-09-01',days[-1],freq='60D'); out=[]
for k0,t0 in enumerate(starts):
    t1=starts[k0+1] if k0+1<len(starts) else days[-1]+pd.Timedelta(days=1)
    tr=X[X.index<=t0-pd.Timedelta(days=12)]; te=X[(X.index>=t0)&(X.index<t1)]
    if not len(te): continue
    o=te[['sym','L','S']].copy()
    for side in ('L','S'):
        m=lgb.LGBMRegressor(n_estimators=300,learning_rate=0.03,num_leaves=15,min_child_samples=100,subsample=0.8,subsample_freq=1,colsample_bytree=0.7,reg_lambda=5,verbose=-1,random_state=k0)
        m.fit(tr[FEAT],tr[side].clip(-1,2)); o['p'+side]=m.predict(te[FEAT])
    out.append(o)
R=pd.concat(out)
print(f"test {R.index.min().date()}..{R.index.max().date()}, rows {len(R)}")
for side in ('L','S'):
    ic=R.groupby(R.index).apply(lambda g:g['p'+side].corr(g[side],method='spearman') if len(g)>=8 else np.nan).dropna()
    icp=R['p'+side].corr(R[side],method='spearman')
    print(f"{side}: pooled rank IC {icp:+.3f} | daily cross-sectional IC {ic.mean():+.3f} (t {ic.mean()/ic.std()*math.sqrt(len(ic)/10):+.2f}, effective n ~ days/10)")
    for q in (0.9,0.97):
        thr=R['p'+side].quantile(q)   # (for a quick look; a causal threshold is used if this passes)
        g=R[R['p'+side]>=thr]; costR=0.0015/R.index.map(lambda t:1).values.mean()
        print(f"   top {100*(1-q):.0f}%: n={len(g)} avg outcome {g[side].mean():+.3f} ATR vs all {R[side].mean():+.3f}  win(+2ATR) {np.mean(g[side]>=2)*100:.0f}%  by year: "+' '.join(f"{y}:{v:+.2f}" for y,v in g.groupby(g.index.year)[side].mean().items()))
imp=pd.Series(m.booster_.feature_importance('gain'),index=FEAT).sort_values(ascending=False); print('top features:',', '.join(imp.index[:12]))
