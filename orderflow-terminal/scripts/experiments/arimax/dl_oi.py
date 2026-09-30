# Hourly open interest (last 5-min value of each hour) from Binance public daily metrics, 12 site coins, 2021-12..2026-08.
import io, zipfile, subprocess, os, numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
B = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/daily/metrics'
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/arimax/oi'
COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'INJUSDT', 'SUIUSDT', 'UNIUSDT']
def day(s, d):
    r = subprocess.run(['curl', '-sf', '--retry', '3', '-m', '60', f'{B}/{s}/{s}-metrics-{d}.zip'], capture_output=True)
    if r.returncode: return None
    try:
        with zipfile.ZipFile(io.BytesIO(r.stdout)) as f: x = pd.read_csv(f.open(f.namelist()[0]), usecols=['create_time', 'sum_open_interest'])
        x['t'] = pd.to_datetime(x.create_time); return x.set_index('t').sum_open_interest.astype(float).resample('h').last()
    except Exception: return None
def one(s):
    out = f'{S}/{s}.pkl'
    if os.path.exists(out): return
    days = [d.strftime('%Y-%m-%d') for d in pd.date_range('2021-12-01', '2026-08-31')]
    with ThreadPoolExecutor(24) as ex: parts = [p for p in ex.map(lambda d: day(s, d), days) if p is not None]
    pd.concat(parts).sort_index().to_pickle(out); print(s, len(parts), flush=True)
for s in COINS: one(s)
print('done oi', flush=True)
