# Binance USDⓈ-M public archive -> 1-second order-flow bars (+ book-depth snapshots) per symbol/day.
import sys, os, io, zipfile, subprocess, datetime as dt, numpy as np, pandas as pd
from concurrent.futures import ProcessPoolExecutor
B='https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/daily'
def get(url):
    r=subprocess.run(['curl','-sf','--retry','4','-m','300',url],capture_output=True)
    return r.stdout if r.returncode==0 else None
def day(args):
    sym,d=args; out=f"{os.environ.get('SEC','sec')}/{sym}_{d}.npz"
    if os.path.exists(out): return out,'cached'
    z=get(f'{B}/aggTrades/{sym}/{sym}-aggTrades-{d}.zip')
    if not z: return out,'no trades'
    with zipfile.ZipFile(io.BytesIO(z)) as f:
        a=pd.read_csv(f.open(f.namelist()[0]),usecols=['price','quantity','transact_time','is_buyer_maker'])
    a['s']=a.transact_time//1000; q=a.quantity.values; p=a.price.values; sell=a.is_buyer_maker.values
    a['bq']=np.where(sell,0,q); a['sq']=np.where(sell,q,0); a['bn']=(~sell).astype(np.int32); a['sn']=sell.astype(np.int32); a['nt']=p*q
    g=a.groupby('s')
    bars=pd.DataFrame({'o':g.price.first(),'h':g.price.max(),'l':g.price.min(),'c':g.price.last(),'bq':g.bq.sum(),'sq':g.sq.sum(),'bn':g.bn.sum(),'sn':g.sn.sum(),'mx':g.nt.max(),
                       'vw':(a.price*a.quantity).groupby(a.s).sum()/g.quantity.sum()})
    dz=get(f'{B}/bookDepth/{sym}/{sym}-bookDepth-{d}.zip'); dep=None
    if dz:
        with zipfile.ZipFile(io.BytesIO(dz)) as f: b=pd.read_csv(f.open(f.namelist()[0]))
        b['s']=((pd.to_datetime(b.timestamp)-pd.Timestamp('1970-01-01')).dt.total_seconds()).astype('int64')
        dep=b.pivot_table(index='s',columns='percentage',values='notional',aggfunc='last')
    np.savez_compressed(out,s=bars.index.values,**{k:bars[k].values for k in bars.columns},
        ds=(dep.index.values if dep is not None else np.array([])),dv=(dep.values if dep is not None else np.zeros((0,10))),dcols=(dep.columns.values.astype(float) if dep is not None else np.array([])))
    return out,f'{len(a)} trades, {len(bars)} secs, depth {0 if dep is None else len(dep)}'
if __name__=='__main__':
    os.makedirs('sec',exist_ok=True)
    SEC=os.environ.get('SEC','sec'); os.makedirs(SEC,exist_ok=True)
    syms=sys.argv[1].split(','); n=int(sys.argv[2]); end=dt.date.fromisoformat(sys.argv[3]) if len(sys.argv)>3 else dt.date(2026,9,25)
    jobs=[(s,(end-dt.timedelta(days=k)).isoformat()) for k in range(n) for s in syms]
    with ProcessPoolExecutor(4) as ex:
        for out,msg in ex.map(day,jobs): print(out,msg,flush=True)
