# LLM intraday trader experiment: data. Binance USDⓈ-M 1-second order-flow bars + book depth (±0.2..5 %, 30 s)
# -> 1-minute bars, daily bars, level-touch events and the market picture a discretionary trader would see at that moment.
import glob, os, numpy as np, pandas as pd
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
SEC = f'{S}/of/sec'

def minute_bars(sym):
    out = []; dep = []
    for f in sorted(glob.glob(f'{SEC}/{sym}_*.npz')):
        z = np.load(f)
        df = pd.DataFrame({k: z[k] for k in ('o', 'h', 'l', 'c', 'bq', 'sq', 'bn', 'sn', 'mx')}, index=pd.to_datetime(z['s'], unit='s'))
        px = df.c
        big = df.mx  # largest single trade notional in the second
        g = df.resample('1min')
        m = pd.DataFrame({'o': g.o.first(), 'h': g.h.max(), 'l': g.l.min(), 'c': g.c.last(), 'bq': g.bq.sum(), 'sq': g.sq.sum(), 'n': (df.bn + df.sn).resample('1min').sum(),
                          'bigmx': big.resample('1min').max()})
        # count of large aggressive trades per side (>= 250k USDT notional in one print) — seconds where the largest trade was big
        isbig = big >= 250_000
        m['bigb'] = (isbig & (df.bq > df.sq)).resample('1min').sum()
        m['bigs'] = (isbig & (df.sq >= df.bq)).resample('1min').sum()
        out.append(m)
        if len(z['ds']):
            d = pd.DataFrame(z['dv'], index=pd.to_datetime(z['ds'], unit='s'), columns=z['dcols'])
            dep.append(d)
    m = pd.concat(out).sort_index()
    m = m[~m.index.duplicated()]
    m['c'] = m.c.ffill()
    for k in ('o', 'h', 'l'): m[k] = m[k].fillna(m.c)
    m = m.fillna(0)
    d = pd.concat(dep).sort_index() if dep else None
    if d is not None:
        d = d[~d.index.duplicated()].resample('1min').last().ffill()
        d = d.reindex(m.index, method='ffill')
    return m, d

def daily(sym, m):
    """Daily bars: the older history from the daily perp file, the rest from the minute bars."""
    base = sym.replace('USDT', '')
    k = pd.read_csv(f'{S}/carry/raw/{base}_perp.csv', header=None)
    t = pd.to_datetime(np.where(k[0] > 1e14, k[0] // 1000, k[0]), unit='ms')
    old = pd.DataFrame({'o': k[1].values, 'h': k[2].values, 'l': k[3].values, 'c': k[4].values}, index=t).groupby(level=0).last()
    g = m.resample('1D')
    new = pd.DataFrame({'o': g.o.first(), 'h': g.h.max(), 'l': g.l.min(), 'c': g.c.last()})
    return pd.concat([old[old.index < new.index[0]], new]).sort_index()

def events(sym, m, dd):
    """First touch per day of: previous-day high/low (all day) and Asia-session (00-08 UTC) high/low (after 08:00).
    Decision time = close of the 5-minute bar containing the touch (the trader sees the first reaction)."""
    ev = []
    days = sorted(set(m.index.normalize()))
    for day in days[1:]:
        prev = dd.loc[:day - pd.Timedelta(seconds=1)].iloc[-1]
        today = m[(m.index >= day) & (m.index < day + pd.Timedelta(days=1))]
        if len(today) < 1000: continue
        asia = today[today.index < day + pd.Timedelta(hours=8)]
        lv = [('PDH', prev.h, today.index[0]), ('PDL', prev.l, today.index[0]),
              ('ASIA_H', asia.h.max(), day + pd.Timedelta(hours=8)), ('ASIA_L', asia.l.min(), day + pd.Timedelta(hours=8))]
        for name, level, since in lv:
            w = today[today.index >= since]
            if not len(w): continue
            # price must start on the right side of the level
            first = w.iloc[0].c
            hi = name.endswith('H')
            if (hi and first >= level) or (not hi and first <= level): continue
            hit = w[w.h >= level] if hi else w[w.l <= level]
            if not len(hit): continue
            t = hit.index[0]
            dec = t.floor('5min') + pd.Timedelta(minutes=5)
            if dec >= today.index[-1] - pd.Timedelta(hours=1): continue
            ev.append({'sym': sym, 'level': name, 'price': float(level), 'touch': t, 'dec': dec})
    return ev

def atr(df, n=14):
    tr = np.maximum(df.h - df.l, np.maximum((df.h - df.c.shift()).abs(), (df.l - df.c.shift()).abs()))
    return tr.rolling(n).mean()
