# Download Binance public daily 'metrics' (5-min OI, top-trader and crowd long/short ratios, taker ratio) and 'bookDepth'
# (±1..5 % depth every ~30 s) for every coin in the months it was in the monthly top-50 (and the month before, for
# lookbacks); each day is reduced at once to a daily summary row. -> met/SYM.pkl, dep/SYM.pkl
import io, zipfile, json, subprocess, os, sys, numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
B = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/daily'
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
UNI = {pd.Timestamp(k): v for k, v in json.load(open(f'{S}/ml_universe.json')).items()}
kind = sys.argv[1]
def get(u):
    r = subprocess.run(['curl', '-sf', '--retry', '3', '-m', '60', u], capture_output=True); return r.stdout if r.returncode == 0 else None
def read(z):
    with zipfile.ZipFile(io.BytesIO(z)) as f: return pd.read_csv(f.open(f.namelist()[0]))
def met_day(s, d):
    z = get(f'{B}/metrics/{s}/{s}-metrics-{d}.zip')
    if not z: return None
    x = read(z)
    if x.empty: return None
    x = x.sort_values('create_time'); oi = x.sum_open_interest.astype(float).values
    h = oi[::12]                                                                    # hourly OI
    dh = np.diff(h) / h[:-1] if len(h) > 2 else np.array([np.nan])
    return dict(day=d, oi=oi[-1], oiv=float(x.sum_open_interest_value.iloc[-1]), oi_open=oi[0],
                oi_min1h=np.nanmin(dh), oi_max1h=np.nanmax(dh),                   # largest hourly OI drop / jump (liquidations / new leverage)
                top_acc=x.count_toptrader_long_short_ratio.astype(float).mean(), top_pos=x.sum_toptrader_long_short_ratio.astype(float).mean(),
                crowd=x.count_long_short_ratio.astype(float).mean(), taker=x.sum_taker_long_short_vol_ratio.astype(float).mean(), n=len(x))
def dep_day(s, d):
    try: return _dep_day(s, d)
    except Exception as e:
        print('dep fail', s, d, str(e)[:80], flush=True); return None
LV = np.array([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5], float)
def _dep_day(s, d):
    z = get(f'{B}/bookDepth/{s}/{s}-bookDepth-{d}.zip')
    if not z: return None
    with zipfile.ZipFile(io.BytesIO(z)) as f: raw = f.open(f.namelist()[0]).read()
    x = pd.read_csv(io.BytesIO(raw), usecols=[0, 1, 3], names=['ts', 'pct', 'notional'], header=0, dtype={'pct': float}, engine='c')
    x = x[x.pct.isin(LV)]                                   # newer files add finer levels; keep ±1..5 %
    if len(x) < 100: return None
    p = x.pivot_table(index='ts', columns='pct', values='notional', aggfunc='last') if not x.ts.is_monotonic_increasing or len(x) % 10 else None
    if p is None:
        pc = x.pct.values; ok = np.all(pc.reshape(-1, 10) == LV, axis=1)
        v = x.notional.values.reshape(-1, 10)[ok]; ts = x.ts.values[::10][ok]
    else:
        p = p.dropna(); p = p[[c for c in LV if c in p.columns]]
        if p.shape[1] < 10: return None
        v = p.values; ts = p.index.values
    if len(v) < 10: return None
    hour = pd.to_datetime(ts).hour
    out = dict(day=d, n=len(v))
    for k in (1, 2, 5):
        b, a = v[:, 5 - k], v[:, 4 + k]; im = (b - a) / (b + a)
        out[f'bid{k}'] = b.mean(); out[f'ask{k}'] = a.mean(); out[f'imb{k}'] = im.mean(); out[f'imb{k}_late'] = im[hour >= 20].mean()
    return out
def one(s):
    out = f'{S}/{"met" if kind == "met" else "dep"}/{s}.pkl'
    if os.path.exists(out): return
    months = set()
    for m, u in UNI.items():
        if s in u: months |= ({m} if kind == 'dep' else {m, m - pd.offsets.MonthBegin(1)})
    days = sorted({d.strftime('%Y-%m-%d') for m in months for d in pd.date_range(m, m + pd.offsets.MonthEnd(0)) if d <= pd.Timestamp('2026-08-31')})
    if kind == 'dep': days = [d for d in days if d >= '2023-01-01']
    fn = met_day if kind == 'met' else dep_day
    with ThreadPoolExecutor(16 if kind == 'dep' else 8) as ex: rows = [r for r in ex.map(lambda d: fn(s, d), days) if r]
    pd.DataFrame(rows).to_pickle(out); print(s, len(days), len(rows), flush=True)
syms = sorted(set(x for u in UNI.values() for x in u))
with ThreadPoolExecutor(6) as ex: list(ex.map(one, syms))
print('done', kind, flush=True)
