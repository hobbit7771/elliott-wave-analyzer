import warnings; warnings.filterwarnings('ignore')
import io,contextlib
src=open('fpred.py').read().split("print('feature")[0]; exec(src)
X['f7dm']=X.groupby('sym').f7.transform(lambda s:s-s.shift(1).rolling(365,min_periods=90).mean())
X['f30']=X.groupby('sym').f1.transform(lambda s:s.rolling(30).mean())
def xs(f,h,a=None,b=None):
    Y=X if a is None else X[(X.index>=a)&(X.index<b)]
    r=[]
    for t,g in Y.groupby(level=0):
        g=g[[f,f'y{h}']].dropna()
        if len(g)>=8: r.append((t,g[f].rank().corr(g[f'y{h}'].rank())))
    s=pd.Series(dict(r)).iloc[::h]; return f"{s.mean():+.3f} (t {s.mean()/s.std()*math.sqrt(len(s)):+.1f})"
for f in ('f7','f30','f7dm'):
    for h in (5,10):
        print(f,h,'all',xs(f,h),'| 2021-23',xs(f,h,'2021','2023-06-01'),'| 2023-26',xs(f,h,'2023-06-01','2027'))
# leave-one-coin-out: is it driven by one coin?
for s in ['LINK','BNB','SOL','INJ','BTC']:
    Y=X[X.sym!=s]; r=[]
    for t,g in Y.groupby(level=0):
        g=g[['f7','y10']].dropna()
        if len(g)>=8: r.append(g.f7.rank().corr(g.y10.rank()))
    r=pd.Series(r).iloc[::10]; print('without',s,f"{r.mean():+.3f} t {r.mean()/r.std()*math.sqrt(len(r)):+.1f}")
