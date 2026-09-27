import warnings; warnings.filterwarnings('ignore')
import io,contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('ftr.py').read().split("run('BASE, funding estimate")[0])
filt=lambda t,s:not (F7.at[t,s]>0.0005)
def run(name, k=None, units=0, add_stop=3.0):
    ps=[]; nb=0; na=0; Rb=[]; Ra=[]
    for s,df in D.items():
        fa=fu[s].fillna(FUND_LONG).values; A=atr(df,20).values
        sig=[x for x in gen(df) if filt(df.index[x['i']],s)]
        tr,p=simulate(df,sig,s,10000,('donchian',10),fund=fa); ps.append(p); nb+=len(tr); Rb+=[t.r for t in tr]
        if not k: continue
        for t in tr:
            j=df.index.get_loc(t.t0); e=df.index.get_loc(t.t1)
            for u in range(1,units+1):
                ent=t.entry+u*k*A[j-1]
                add=dict(i=j,dir=1,entry=ent,stop=ent-add_stop*A[j-1],target=None,valid=max(1,e-j),why='ADD')
                # the add-on can only live while the base position is open: cap its life at the base exit
                tr2,p2=simulate(df.iloc[:e+1],[add],s,10000,('donchian',10),fund=fa[:e+1])
                if tr2: ps.append(p2.reindex(df.index).fillna(0)); na+=1; Ra+=[x.r for x in tr2]
    P=pd.concat(ps,axis=1,sort=True).fillna(0).sum(axis=1); P=P[P.index>='2021-06-01']
    m=metrics(P); f=lambda x:x.mean()/x.std()*math.sqrt(365); h1=P[P.index<'2023-06-01']; h2=P[P.index>='2023-06-01']
    yrs=P.groupby(P.index.year).sum()
    print(f"{name:34s} base {nb} +adds {na} (avgR {np.mean(Ra) if Ra else 0:+.2f}) | SR {m['sr']:.2f} sumR {P.sum():+5.0f} maxDD {m['mdd']:4.0f}R ret/DD {P.sum()/m['mdd']:.1f} halves {f(h1):+.2f}/{f(h2):+.2f} | "+' '.join(f"{y}:{v:+.0f}" for y,v in yrs.items()),flush=True)
run('SITE (funding filter)')
for k in (0.5,1.0,2.0):
    for u in (1,2,3):
        run(f'pyramid +{u} units every {k} ATR',k,u)
