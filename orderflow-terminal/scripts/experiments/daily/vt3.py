import warnings; warnings.filterwarnings('ignore')
import io,contextlib
with contextlib.redirect_stdout(io.StringIO()):
    from vt import *
v=P.rolling(30).std().shift(1); ref=v.expanding(180).median(); Q=P*(ref/v).clip(upper=1).fillna(1)
A=P[P.index>='2021-06-01'].values; B=Q[Q.index>='2021-06-01'].values; n=len(A)
sr=lambda x:x.mean()/x.std()*math.sqrt(365)
rng=np.random.default_rng(7); d=[]
for _ in range(2000):
    idx=[]
    while len(idx)<n:
        s=rng.integers(0,n); L=rng.geometric(1/20); idx.extend(((s+np.arange(L))%n).tolist())
    idx=idx[:n]; d.append(sr(B[idx])-sr(A[idx]))
d=np.array(d); print('SR diff', round(sr(B)-sr(A),2),'CI90',np.percentile(d,[5,95]).round(2),'P(diff<=0)',(d<=0).mean())
# yearly
for y in range(2021,2027):
    a=P[P.index.year==y]; b=Q[Q.index.year==y]; print(y, round(sr(a.values),2), round(sr(b.values),2), 'exposure k mean', round(((ref/v).clip(upper=1).fillna(1))[P.index.year==y].mean(),2))
