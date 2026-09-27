import warnings; warnings.filterwarnings('ignore')
from dbt import *
import pickle
variants,M,trades=pickle.load(open('wf_don.pkl','rb'))
D=load()
M=M.loc['2021-06-01':]
starts=pd.date_range('2023-06-01','2026-06-01',freq='182D')
oos=[]; chosen=[]
for s in starts:
    tr=M.loc[s-pd.Timedelta(days=730):s-pd.Timedelta(days=1)]
    te=M.loc[s:s+pd.Timedelta(days=181)]
    if len(te)==0: continue
    sr=tr.mean()/tr.std()
    k=int(sr.idxmax()); chosen.append((s.date(),k,round(sr[k]*np.sqrt(365),2)))
    oos.append(te[k])
O=pd.concat(oos)
print('chosen per window (start, variant, train SR):'); 
for c in chosen: print('  ',c[0],variants[c[1]],'trainSR',c[2])
m=metrics(O); p=block_boot_p(O)
dsr,sr0=deflated_sr(m['sr'],len(O),len(variants),skew=O.skew(),kurt=O.kurt()+3)
print(f"\nWALK-FORWARD OOS {O.index[0].date()}→{O.index[-1].date()}: SR {m['sr']:.2f}, sumR {O.sum():+.0f}, maxDD {m['mdd']:.0f}R, block-bootstrap p(mean<=0) {p:.3f}, deflated-SR prob {dsr:.2f} (SR needed to beat {len(variants)} noise trials ≈ {sr0:.2f})")
print('OOS by half-year:', ' '.join(f"{i.date()}:{v:+.0f}" for i,v in O.resample('182D').sum().items()))
# fixed Turtle 20/10 long+short without selection, same OOS span, for reference
k2010=[i for i,v in enumerate(variants) if v==dict(n_in=20,n_out=10,stop_atr=2.0,long_only=False,btc=False,er_min=None)][0]
F=M[k2010].loc[O.index[0]:]; mf=metrics(F)
print(f"fixed Turtle 20/10 (no selection) same span: SR {mf['sr']:.2f} sumR {F.sum():+.0f} maxDD {mf['mdd']:.0f}R")
# average variant (robustness of the family)
avg=M.loc[O.index[0]:].mean(axis=1); ma=metrics(avg)
print(f"average of all {len(variants)} variants same span: SR {ma['sr']:.2f}; share of variants with SR>0 over the whole period: {(M.mean()>0).mean():.2f}")
# benchmark: equal-risk buy & hold of the basket (1R = 1 daily ATR-sized position? use daily returns / ATR% as R)
bh=[]
for s,df in D.items():
    a=atr(df)/df.c; r=df.c.pct_change()/ (2*a.shift())  # position sized so a 2-ATR move = 1R, like a 2-ATR stop
    bh.append(r.rename(s))
B=pd.concat(bh,axis=1,sort=True).fillna(0).sum(axis=1).loc[O.index[0]:O.index[-1]]
mb=metrics(B)
print(f"benchmark buy&hold basket (2-ATR risk units) same span: SR {mb['sr']:.2f} sumR {B.sum():+.0f} maxDD {mb['mdd']:.0f}R; correlation strategy~benchmark {np.corrcoef(O.reindex(B.index).fillna(0),B)[0,1]:.2f}")
