# Binance USDⓈ-M monthly + daily kline archives (data.vision) -> data5y/SYM_15m.json and SYM_1d.json ([t,o,h,l,c,v]).
import io, zipfile, json, subprocess, datetime as dt, sys
from concurrent.futures import ThreadPoolExecutor
B = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um'
SYMS = 'BTC ETH SOL XRP DOGE BNB ADA LINK AVAX SUI UNI INJ'.split()
def get(u):
    r = subprocess.run(['curl', '-sf', '--retry', '3', '-m', '120', u], capture_output=True); return r.stdout if r.returncode == 0 else None
def rows(z):
    if not z: return []
    with zipfile.ZipFile(io.BytesIO(z)) as f:
        out = []
        for line in f.open(f.namelist()[0]).read().decode().splitlines():
            p = line.split(',')
            if not p[0].isdigit(): continue
            out.append([int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])])
        return out
months = [(y, m) for y in range(2020, 2027) for m in range(1, 13) if (y, m) <= (2026, 8)]
days = [dt.date(2026, 9, 1) + dt.timedelta(d) for d in range(25)]
def one(sym, tf):
    s = sym + 'USDT'; urls = [f'{B}/monthly/klines/{s}/{tf}/{s}-{tf}-{y}-{m:02d}.zip' for y, m in months] + [f'{B}/daily/klines/{s}/{tf}/{s}-{tf}-{d}.zip' for d in days]
    with ThreadPoolExecutor(8) as ex: parts = list(ex.map(lambda u: rows(get(u)), urls))
    allr = sorted({r[0]: r for p in parts for r in p}.values())
    json.dump(allr, open(f'data5y/{s}_{tf}.json', 'w'))
    return s, tf, len(allr), dt.datetime.utcfromtimestamp(allr[0][0] / 1000).date() if allr else None
for sym in SYMS:
    for tf in ('1d', '15m'): print(one(sym, tf), flush=True)
