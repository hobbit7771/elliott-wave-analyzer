import json, glob, os, numpy as np, pandas as pd
closes={}
for f in sorted(glob.glob('daily/*.json')):
    d=np.array(json.load(open(f))); s=os.path.basename(f)[:-5].replace('USDT','')
    closes[s]=pd.Series(d[:,4], index=pd.to_datetime(d[:,0], unit='ms'))
C=pd.DataFrame(closes).sort_index()
R=np.log(C).diff()
print('coins',C.shape[1],'days',C.shape[0], C.index[0].date(),'→',C.index[-1].date())
# 1) autocorrelation of daily returns, pooled (mean over coins) with rough SE
print('\n1) Autocorrelation of daily log returns (mean over coins; SE≈1/sqrt(N))')
for lag in [1,2,3,5,10]:
    ac=[R[c].dropna().autocorr(lag) for c in R]
    n=np.mean([R[c].dropna().size for c in R])
    print(f'  lag {lag:2d}: mean {np.mean(ac):+.3f}  (coins >0: {sum(a>0 for a in ac)}/{len(ac)}; SE of one coin ~{1/np.sqrt(n):.3f})')
# 2) Variance ratio (Lo–MacKinlay, heteroskedasticity-robust z*)
def vr(x,q):
    x=x.dropna().values; n=len(x); mu=x.mean()
    s1=((x-mu)**2).sum()/(n-1)
    xq=np.convolve(x,np.ones(q),'valid'); m=q*(n-q+1)*(1-q/n)
    sq=((xq-q*mu)**2).sum()/m
    V=sq/s1
    # robust variance of VR
    e=(x-mu)**2; th=0
    for j in range(1,q):
        d=((e[j:]*e[:-j]).sum())/(e.sum()**2)*n
        th+=(2*(q-j)/q)**2*d
    return V,(V-1)/np.sqrt(th)
print('\n2) Variance ratio VR(q) (>1 trend, <1 mean reversion); median over coins, count |z*|>1.96')
for q in [2,5,10,20,60]:
    vs=[vr(R[c],q) for c in R]
    print(f'  q={q:2d}: median VR {np.median([v for v,_ in vs]):.3f}; z*>1.96: {sum(z>1.96 for _,z in vs)}, z*<-1.96: {sum(z<-1.96 for _,z in vs)} of {len(vs)}')
# 3) Time-series momentum: sign(past k-day return) × next h-day return, pooled, t-stat clustered by date (mean across coins per date)
print('\n3) TSMOM predictive test: mean of sign(r_past k) × r_next h (per-date cross-coin average → t over dates)')
for k in [5,10,20,60,120]:
    row=[]
    for h in [1,5,20]:
        past=np.log(C).diff(k); fut=np.log(C).shift(-h)-np.log(C)
        vol=R.rolling(60).std()
        x=(np.sign(past)*fut/(vol*np.sqrt(h))).mean(axis=1).dropna()
        # non-overlapping by taking every h-th date
        x=x.iloc[::h]
        t=x.mean()/x.std()*np.sqrt(len(x))
        row.append(f'h={h:2d}: {x.mean():+.3f} (t {t:+.1f})')
    print(f'  k={k:3d}: '+' | '.join(row))
# 4) volatility clustering
print('\n4) Volatility clustering: autocorr of |r| at lag 1 / 5 / 20 (mean over coins)')
print('  ', [round(np.mean([R[c].abs().dropna().autocorr(l) for c in R]),3) for l in [1,5,20]])
# 5) average pairwise correlation of daily returns
cm=R.corr().values; iu=np.triu_indices_from(cm,1)
print('\n5) Mean pairwise correlation of daily returns:', round(np.nanmean(cm[iu]),3), '→ effective number of independent coins ≈', round(C.shape[1]/(1+(C.shape[1]-1)*np.nanmean(cm[iu])),1))
# 6) fat tails
k=[pd.Series(R[c].dropna()).kurt() for c in R]
print('6) Excess kurtosis of daily returns: median', round(np.median(k),1), '(normal = 0)')
