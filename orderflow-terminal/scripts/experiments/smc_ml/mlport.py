import pandas as pd, numpy as np, sys
COST=0.00075
D=pd.read_pickle('ml_preds.pkl').dropna(subset=['pred'])
D['ret']=np.exp(D.y)-1
R=D.pivot_table(index='t',columns='sym',values='ret')      # daily 24h returns from 00:00 (NaN outside the universe)
PR=D.pivot_table(index='t',columns='sym',values='pred'); V=D.pivot_table(index='t',columns='sym',values='vol')
def run(t0,t1,N,K,rule):
    days=R.index[(R.index>=t0)&(R.index<t1)]; w=pd.Series(dtype=float); out=[]
    for i,t in enumerate(days):
        if i%N==0:
            p=PR.loc[t].dropna()
            if len(p)>=2*K:
                p=p.sort_values(); iv=1/V.loc[t]
                L=p.index[-K:]; S=p.index[:K]; nw={}
                if rule=='long_only': L=[s for s in L if p[s]>0]
                if len(L): wl=iv[L]/iv[L].sum(); nw.update(wl.to_dict())
                if rule=='long_short': ws=iv[S]/iv[S].sum(); nw.update((-ws).to_dict())
                nw=pd.Series(nw); turn=nw.sub(w,fill_value=0).abs().sum(); w=nw
            else: turn=0
        else: turn=0
        r=R.loc[t].reindex(w.index).fillna(0)
        out.append((t,(w*r).sum()-COST*turn))
        w=w*(1+r)  # drift
        if len(w): w=w/ (w.abs().sum()/ (2 if rule=='long_short' else 1)) if w.abs().sum()>0 else w
    s=pd.Series(dict(out)); eq=(1+s).cumprod(); yrs=len(s)/365
    return s, eq.iloc[-1]-1, eq.iloc[-1]**(1/yrs)-1, s.mean()/s.std()*np.sqrt(365), (1-eq/eq.cummax()).max()
if __name__=='__main__':
    per=sys.argv[1]
    t0,t1=(pd.Timestamp('2022-07-01'),pd.Timestamp('2025-10-01')) if per=='dev' else (pd.Timestamp('2025-10-01'),pd.Timestamp('2026-09-01'))
    rows=[]
    combos=[(N,K,rule) for N in (1,3,7) for K in (5,10) for rule in ('long_short','long_only')] if per=='dev' else [tuple(eval(sys.argv[2]))]
    for N,K,rule in combos:
        s,tot,cagr,sh,dd=run(t0,t1,N,K,rule); rows.append((f'rebalance {N}d K={K} {rule}',round(tot*100,1),round(cagr*100,1),round(sh,2),round(dd*100,1)))
    print(pd.DataFrame(rows,columns=['variant','total%','CAGR%','Sharpe','maxDD%']).to_string(index=False))
