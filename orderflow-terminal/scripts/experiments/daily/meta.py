# Meta-labeling (López de Prado): should the trend model take THIS breakout? Walk-forward on past closed trades only.
import warnings; warnings.filterwarnings('ignore')
import io,contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('ftr.py').read().split("run('BASE, funding estimate")[0])
import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
Pn=pd.read_pickle('panel.pkl')
FEAT=[c for c in Pn.columns if c not in ('sym','age','y1','y5','y10','n_coins')]
filt=lambda t,s:not (F7.at[t,s]>0.0005)
# all trades of the current site trend model (with the funding filter), with the day-by-day MTM pnl kept per trade
rows=[]; mtm={}
for s,df in D.items():
    sig=[x for x in gen(df) if filt(df.index[x['i']],s)]
    fa=fu[s].fillna(FUND_LONG).values
    for x in sig:
        tr,p=simulate(df,[x],s,10000,('donchian',10),fund=fa)
        if not tr: continue
        t=tr[0]; sd=df.index[x['i']]
        # simulate() of a single signal ignores overlap; keep only trades that the real (one-position) engine takes
        rows.append(dict(sym=s,sig=sd,t0=t.t0,t1=t.t1,r=t.r)); mtm[(s,t.t0)]=p
T=pd.DataFrame(rows).sort_values('t0')
# enforce one position per coin at a time (same as the engine)
keep=[]; busy={}
for i,x in T.iterrows():
    if busy.get(x.sym,pd.Timestamp(0))>=x.t0: continue
    keep.append(i); busy[x.sym]=x.t1
T=T.loc[keep].reset_index(drop=True)
X=Pn.reset_index().set_index(['t','sym'])[FEAT]
T=T.join(X,on=['sig','sym'])
print('trades',len(T),'avgR %+.2f'%T.r.mean())
def wf(model,minN=120,feats=FEAT):
    p=np.full(len(T),np.nan)
    for i in range(len(T)):
        tr=T[T.t1<T.sig.iloc[i]]            # only trades already CLOSED at the signal
        if len(tr)<minN: continue
        mu=tr[feats].median(); sd=tr[feats].std().replace(0,1)
        Xtr=((tr[feats]-mu)/sd).clip(-5,5).fillna(0).values; x=((T[feats].iloc[[i]]-mu)/sd).clip(-5,5).fillna(0).values
        y=(tr.r>0).astype(int).values
        if model=='logit': p[i]=LogisticRegression(C=0.05,max_iter=500).fit(Xtr,y).predict_proba(x)[0,1]
        elif model=='logit_small': p[i]=LogisticRegression(C=0.2,max_iter=500).fit(Xtr,y).predict_proba(x)[0,1]
        else: p[i]=lgb.LGBMClassifier(n_estimators=150,learning_rate=0.03,num_leaves=4,min_child_samples=30,subsample=0.8,subsample_freq=1,colsample_bytree=0.6,verbose=-1,random_state=1).fit(Xtr,y).predict_proba(x)[0,1]
    return p
def portfolio(mult):
    ps=[]
    for (i,x),m in zip(T.iterrows(),mult):
        if m>0: ps.append(mtm[(x.sym,x.t0)]*m)
    P=pd.concat(ps,axis=1).fillna(0).sum(axis=1); P=P[P.index>='2021-06-01']
    return P
def rep(name,mult,mask):
    P=portfolio(mult); m=metrics(P); f=lambda x:x.mean()/x.std()*math.sqrt(365)
    h1=P[P.index<'2023-06-01']; h2=P[P.index>='2023-06-01']
    sel=T[mask&(mult>0)]
    print(f"{name:40s} trades {int((mult>0)[mask].sum()):3d}/{int(mask.sum())} avgR {sel.r.mean():+.2f} | portfolio SR {m['sr']:.2f} sumR {P.sum():+.0f} DD {m['mdd']:.0f} halves {f(h1):+.2f}/{f(h2):+.2f}")
small=['r7','r30','r60','pos55','dhi55','volr','vr5','f7','oi7','btc_r30','btc_sma50','xs_r30','sma50','sma200','rng']
for name,model,feats in [('logit all',"logit",FEAT),('logit 15 feats','logit_small',small),('lgbm all','lgbm',FEAT)]:
    p=wf(model,feats=feats); T['p_'+model]=p; ok=~np.isnan(p)
    base=np.where(ok,1.0,0.0)
    print(f"== {name}: AUC-like rank corr p vs win on scored trades: {pd.Series(p[ok]).rank().corr(pd.Series((T.r[ok]>0).astype(int).values).rank()):+.3f}")
    rep('  scored trades, all taken',base,ok)
    for q in (0.3,0.5):
        thr=pd.Series(p).expanding().quantile(q).shift(1).values  # threshold from past predictions only
        rep(f'  skip if p < past {int(q*100)}th pct',np.where(ok&(p>=thr),1.0,0.0),ok)
    rep('  bet sizing: weight = p/mean(p)',np.where(ok,p/np.nanmean(p),0.0),ok)
