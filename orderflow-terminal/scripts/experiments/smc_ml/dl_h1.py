# 1h klines (with taker-buy volume) + funding for every Binance USDⓈ-M perp that traded between 2021-07 and 2025-06
# (delisted ones included, to avoid survivorship bias) -> h1/SYM.npz
import io, zipfile, json, subprocess, os, numpy as np
from concurrent.futures import ThreadPoolExecutor
B = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um'
res = json.load(open('perp_months.json'))
cand = [s for s, m in res.items() if m and m[0] <= '2025-06' and m[-1] >= '2021-07']
def get(u):
    r = subprocess.run(['curl', '-sf', '--retry', '3', '-m', '120', u], capture_output=True); return r.stdout if r.returncode == 0 else None
def csv(z):
    if not z: return []
    with zipfile.ZipFile(io.BytesIO(z)) as f: return [l.split(',') for l in f.open(f.namelist()[0]).read().decode().splitlines() if l[:1].isdigit()]
def one(s):
    out = f'h1/{s}.npz'
    if os.path.exists(out): return
    ms = [m for m in res[s] if m >= '2021-01']
    k = []; fr = []
    for m in ms:
        for p in csv(get(f'{B}/monthly/klines/{s}/1h/{s}-1h-{m}.zip')):
            k.append([int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[7]), float(p[9])])  # t o h l c vol quoteVol takerBuyBase
        for p in csv(get(f'{B}/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip')):
            fr.append([int(p[0]), float(p[2])])
    if not k: return
    k = np.array(sorted({r[0]: r for r in k}.values())); fr = np.array(sorted(fr)) if fr else np.zeros((0, 2))
    np.savez_compressed(out, k=k, f=fr)
with ThreadPoolExecutor(12) as ex: list(ex.map(one, cand))
print('done', len(os.listdir('h1')))
