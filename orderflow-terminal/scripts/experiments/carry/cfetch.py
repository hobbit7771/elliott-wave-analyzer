# Binance public archive: monthly funding, spot 1d klines, USDⓈ-M perp 1d klines -> one CSV per symbol and kind.
import io, os, zipfile, subprocess, pandas as pd, datetime as dt
from concurrent.futures import ThreadPoolExecutor
B='https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data'
SYMS='BTC ETH BNB XRP ADA SOL DOGE DOT LTC LINK BCH AVAX TRX ETC XLM ATOM FIL UNI AAVE NEAR ALGO EOS SAND MANA AXS ICP APT ARB OP SUI INJ TIA SEI WLD LDO'.split()
months=pd.period_range('2020-01','2026-08',freq='M').astype(str)
def get(url):
    r=subprocess.run(['curl','-sf','--retry','3','-m','120',url],capture_output=True); return r.stdout if r.returncode==0 else None
def one(args):
    s,kind,m=args; sym=s+'USDT'
    url={'fund':f'{B}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip',
         'spot':f'{B}/spot/monthly/klines/{sym}/1d/{sym}-1d-{m}.zip',
         'perp':f'{B}/futures/um/monthly/klines/{sym}/1d/{sym}-1d-{m}.zip'}[kind]
    z=get(url)
    if not z: return None
    with zipfile.ZipFile(io.BytesIO(z)) as f: raw=f.open(f.namelist()[0]).read().decode()
    lines=[l for l in raw.splitlines() if l and l[0].isdigit()]
    return (s,kind,lines)
os.makedirs('raw',exist_ok=True)
jobs=[(s,k,m) for s in SYMS for k in ('fund','spot','perp') for m in months]
acc={}
with ThreadPoolExecutor(16) as ex:
    for r in ex.map(one,jobs):
        if r: acc.setdefault((r[0],r[1]),[]).extend(r[2])
for (s,k),lines in acc.items():
    open(f'raw/{s}_{k}.csv','w').write('\n'.join(lines))
print(len(acc),'series;', sorted({s for s,_ in acc}))
