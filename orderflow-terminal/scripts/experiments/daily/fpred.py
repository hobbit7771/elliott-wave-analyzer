import warnings; warnings.filterwarnings('ignore')
from dbt import *
D=load()
def feats(s):
    df=D[s].copy(); dn=(df.index-pd.Timestamp('1970-01-01')).days
    fu=dict(json.load(open(f'fund/{s}.json'))); oi=dict(json.load(open(f'oi/{s}.json')))
    df['f1']=[fu.get(int(d),np.nan)/1e6 for d in dn]
    df['oi']=[oi.get(int(d),np.nan) for d in dn]   # snapshot at the start of the day: known at the close
    df['f7']=df.f1.rolling(7).mean(); df['fz']=(df.f7-df.f7.rolling(90).mean())/df.f7.rolling(90).std()
    df['oi7']=df.oi/df.oi.shift(7)-1; df['r7']=df.c/df.c.shift(7)-1; df['r1']=df.c/df.c.shift(1)-1
    df['oi1']=df.oi/df.oi.shift(1)-1
    for h in (1,5,10,20): df[f'y{h}']=df.o.shift(-1-h)/df.o.shift(-1)-1   # entry next open
    df['sym']=s; return df
X=pd.concat([feats(s) for s in D]); X=X[X.index>='2021-06-01']
def ts_test(f, h):
    """time-series: per coin, correlate feature with future h-day return; pooled, non-overlapping samples every h days."""
    ics=[]
    for s,g in X.groupby('sym'):
        g=g[[f,f'y{h}']].dropna().iloc[::h]
        if len(g)>30: ics.append((g[f].rank().corr(g[f'y{h}'].rank()), len(g)))
    ic=np.array([a for a,_ in ics]); n=sum(b for _,b in ics)
    return ic.mean(), ic.mean()*math.sqrt(n/1.6/ max(1,len(ics)) * len(ics)/max(1,len(ics)))  # rough
def xs_test(f,h):
    """cross-section: each day rank coins by feature, correlate with future h-day return; mean IC and t over non-overlapping days."""
    r=[]
    for t,g in X.groupby(level=0):
        g=g[[f,f'y{h}']].dropna()
        if len(g)>=8: r.append((t,g[f].rank().corr(g[f'y{h}'].rank())))
    s=pd.Series(dict(r)).iloc[::h]; return s.mean(), s.mean()/s.std()*math.sqrt(len(s)), len(s)
print('feature   h | TS mean IC | XS mean IC  t  n')
for f in ('f1','f7','fz','oi7','oi1','r7'):
    for h in (1,5,10,20):
        a,_=ts_test(f,h); b,t,n=xs_test(f,h)
        print(f"{f:5s} {h:3d} | {a:+.3f}     | {b:+.3f} {t:+5.2f} {n}")
# quintile sort on funding level (pooled time-series): future 10d return
for f in ('f7','fz'):
    g=X[[f,'y10']].dropna(); q=pd.qcut(g[f],5,labels=False)
    print(f, 'quintile mean 10d ret %:', ' '.join(f"{v*100:+.2f}" for v in g.groupby(q).y10.mean()))
