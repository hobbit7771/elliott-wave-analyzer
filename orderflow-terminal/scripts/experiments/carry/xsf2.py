import math, numpy as np, pandas as pd, os, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    exec(open('xsf.py').read().split("for lb in")[0])
base=run(lb=7,hold=7,name='base lb7 hold7 (weekly)')
btc=R['BTC'].loc[base.index]
print('beta to BTC:', round(np.cov(base,btc)[0,1]/btc.var(),3), ' corr:', round(base.corr(btc),3))
run(lb=7,hold=7,start='2022-01-01',name='from 2022 (no 2021)')
run(lb=7,hold=7,cost=0.0015,name='costs x2 (0.15%/side)')
run(lb=7,hold=7,q=0.33,name='terciles')
# leave-one-coin-out: drop each coin and see the Sharpe
res={}
allF,allP=F.copy(),P.copy()
for c in list(allF.columns):
    F=allF.drop(columns=c); P=allP.drop(columns=c); R=P.pct_change(); vol=R.rolling(30,min_periods=20).std()*math.sqrt(365)
    with contextlib.redirect_stdout(io.StringIO()): r=run(lb=7,hold=7)
    res[c]=r.mean()/r.std()*math.sqrt(365)
s=pd.Series(res).sort_values(); print('leave-one-out Sharpe: min', s.head(4).round(2).to_dict(), ' max', s.tail(3).round(2).to_dict())
# only coins listed before 2021 (no newly listed coins)
F=allF[[c for c in allF.columns if allF[c].loc[:'2020-12-31'].notna().sum()>100]]; P=allP[F.columns]; R=P.pct_change(); vol=R.rolling(30,min_periods=20).std()*math.sqrt(365)
print('old coins only:',len(F.columns)); run(lb=7,hold=7,name='only coins listed before 2021')
