# SMC / ICT model, strict mechanical rules (15m execution, 4h bias), Binance USDⓈ-M 15m bars, 12 coins, 2021-06 .. 2026-09.
#
# Definitions (all causal: a swing point is known only after its confirmation bars have CLOSED):
#   Swing high at bar j: high[j] is strictly greater than the highs of the N bars on each side (fractal, N = 2);
#   it is confirmed at the close of bar j + N. Swing low symmetric.
#   Fair value gap (FVG), bullish at bar i: low[i] > high[i-2] (the gap between bar i-2's high and bar i's low);
#   its consequent encroachment (CE) is the middle of the gap. Bearish: high[i] < low[i-2].
#   Order block (OB), bullish: the last down-close candle (close < open) at or before the sweep bar; zone = its
#   open..low, entry at its open (proximal line). Bearish symmetric (last up-close candle, entry at its open).
#
# HTF bias (4h, closed bars only): the direction of the last break of structure — a 4h close above the last confirmed
#   4h swing high makes the bias bullish, a close below the last confirmed 4h swing low bearish.
#
# LONG setup (short is the mirror image):
#   1) Liquidity sweep: a 15m bar trades below the last confirmed 15m swing low (sell-side liquidity taken).
#      The sweep low = the lowest low from that bar until the structure shift.
#   2) Market structure shift (MSS / CHoCH) with displacement: within 12 bars (3 h) after the sweep, a bar CLOSES above
#      the last confirmed 15m swing high that existed at the sweep; the leg from the sweep low to the MSS bar must
#      contain at least one bullish FVG.
#   3) Premium/discount: the entry must be in the discount half of the leg (at or below 50 % of sweep low .. leg high).
#   4) Entry: a limit order, placed at the close of the MSS bar, at
#        'fvg': the CE of the last bullish FVG of the leg that is in discount, or
#        'ob' : the open of the order block;
#      valid for 24 bars (6 h); cancelled if price reaches the target first.
#   5) Stop: below the sweep low by 0.1 × ATR(14, 15m).
#   6) Target: '2R', '3R', or 'liq' = the last confirmed 4h swing high above the entry (buy-side liquidity);
#      a setup with less than 2 R of room to that target is skipped.
#   7) Filters: 'bias' — trade only with the 4h bias; 'kz' — the MSS bar is in the London (07–10 UTC) or New York
#      (12–15 UTC) kill zone.
#   8) One position per coin; exit after 48 h at the close if neither stop nor target was hit; the stop is assumed
#      first when a bar touches both; the entry bar can hit the stop but not the target.
#   Costs: Bybit fees — limit entry and target exits maker 0.02 %, stop and time exits taker 0.055 % + 0.02 % slippage.
import json, sys, itertools, numpy as np, pandas as pd
import os
TF = os.environ.get('TF', '15m'); HTF_MS = {'15m': 4 * 3600 * 1000, '1h': 86400 * 1000}[TF]
N = 2; MSS_BARS = 12; VALID = 24; HOLD = 192 if TF == '15m' else 120; FM, FT, SLIP = 0.0002, 0.00055, 0.0002
SYMS = 'BTC ETH SOL XRP DOGE BNB ADA LINK AVAX SUI UNI INJ'.split()
START = pd.Timestamp('2021-06-01').value // 10**6

def load(s):
    a = np.array(json.load(open(f'data5y/{s}USDT_15m.json')))
    if TF == '1h':
        df = pd.DataFrame(a[:, :5], columns=['t', 'o', 'h', 'l', 'c']); df['g'] = (df.t // 3600000).astype(np.int64)
        g = df.groupby('g').agg(t=('t', 'first'), o=('o', 'first'), h=('h', 'max'), l=('l', 'min'), c=('c', 'last'))
        return g.t.values.astype(np.int64), g.o.values, g.h.values, g.l.values, g.c.values
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]

def swings(h, l, n=N):
    """confirmed swing highs/lows: arrays of (bar index j, confirm index j+n, price)"""
    L = len(h); sh = []; sl = []
    for j in range(n, L - n):
        if h[j] > h[j - n:j].max() and h[j] > h[j + 1:j + n + 1].max(): sh.append((j, j + n, h[j]))
        if l[j] < l[j - n:j].min() and l[j] < l[j + 1:j + n + 1].min(): sl.append((j, j + n, l[j]))
    return sh, sl

def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]; tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().values

def htf(t, o, h, l, c):
    """4h bars from 15m, then per 15m bar: bias (+1/-1/0) and the lists of confirmed 4h swing highs/lows known at that bar"""
    df = pd.DataFrame({'t': t, 'o': o, 'h': h, 'l': l, 'c': c}); df['g'] = df.t // HTF_MS
    g = df.groupby('g').agg(t=('t', 'first'), h=('h', 'max'), l=('l', 'min'), c=('c', 'last'))
    H, Lo, C = g.h.values, g.l.values, g.c.values; close_t = (g.index.values + 1) * HTF_MS
    sh, sl = swings(H, Lo)
    bias = np.zeros(len(g)); b = 0; ish = isl = 0; lastH = lastL = None
    for k in range(len(g)):
        while ish < len(sh) and sh[ish][1] <= k: lastH = sh[ish][2]; ish += 1
        while isl < len(sl) and sl[isl][1] <= k: lastL = sl[isl][2]; isl += 1
        # use swings confirmed at or before the previous closed bar vs this bar's close
        if lastH is not None and C[k] > lastH: b = 1
        if lastL is not None and C[k] < lastL: b = -1
        bias[k] = b
    # map to 15m: a 15m bar at time t sees the last 4h bar that CLOSED at or before t
    idx = np.searchsorted(close_t, t, side='right') - 1
    b15 = np.where(idx >= 0, bias[np.clip(idx, 0, None)], 0)
    shp = [(close_t[x[1]], x[2]) for x in sh]; slp = [(close_t[x[1]], x[2]) for x in sl]   # (time known, price)
    return b15, shp, slp

def setups(s):
    t, o, h, l, c = load(s); A = atr(h, l, c); b4, sh4, sl4 = htf(t, o, h, l, c)
    sh, sl = swings(h, l)
    out = []
    # state: last confirmed swing high / low as of each bar
    ish = isl = 0; lastSH = lastSL = None
    shT = np.array([x[0] for x in sh4]); shP = np.array([x[1] for x in sh4]); slT = np.array([x[0] for x in sl4]); slP = np.array([x[1] for x in sl4])
    i = 0; L = len(t); busy_until = -1
    while i < L - 1:
        while ish < len(sh) and sh[ish][1] <= i: lastSH = sh[ish]; ish += 1
        while isl < len(sl) and sl[isl][1] <= i: lastSL = sl[isl]; isl += 1
        found = False
        if t[i] >= START and lastSH and lastSL:
            for d in (1, -1):
                ref = lastSL if d > 0 else lastSH                     # liquidity pool
                opp = lastSH if d > 0 else lastSL                     # structure level for the shift
                if not ((l[i] < ref[2]) if d > 0 else (h[i] > ref[2])): continue
                ext = l[i] if d > 0 else h[i]; mss = None
                for k in range(i + 1, min(L, i + 1 + MSS_BARS)):
                    ext = min(ext, l[k]) if d > 0 else max(ext, h[k])
                    if (c[k] > opp[2]) if d > 0 else (c[k] < opp[2]): mss = k; break
                if mss is None: continue
                # leg: from the extreme bar to the MSS bar
                seg = slice(i, mss + 1); legHi = h[seg].max() if d > 0 else l[seg].min()
                eq = (ext + legHi) / 2
                fvgs = []
                for k in range(i + 2, mss + 1):
                    if d > 0 and l[k] > h[k - 2]: fvgs.append((k, (l[k] + h[k - 2]) / 2))
                    if d < 0 and h[k] < l[k - 2]: fvgs.append((k, (h[k] + l[k - 2]) / 2))
                if not fvgs: continue
                fvg_disc = [x for x in fvgs if (x[1] <= eq if d > 0 else x[1] >= eq)]
                ob = None
                for k in range(i, max(i - 20, 0), -1):
                    if (c[k] < o[k]) if d > 0 else (c[k] > o[k]): ob = o[k]; break
                hour = pd.Timestamp(t[mss], unit='ms').hour
                sw = sh4 if d > 0 else sl4
                # buy-side liquidity target: last confirmed 4h swing high (known at the MSS bar) above the leg
                T_, P_ = (shT, shP) if d > 0 else (slT, slP)
                known = P_[T_ <= t[mss]]
                cand = known[known > legHi] if d > 0 else known[known < legHi]
                liq = (cand[-1] if len(cand) else np.nan)
                out.append(dict(sym=s, i=i, mss=mss, d=d, t=int(t[mss]), stop=ext - d * 0.1 * A[mss], eq=eq,
                                fvg=(fvg_disc[-1][1] if fvg_disc else np.nan), ob=(ob if ob is not None and ((ob <= eq) if d > 0 else (ob >= eq)) else np.nan),
                                liq=liq, bias=int(b4[mss]), kz=(7 <= hour < 10) or (12 <= hour < 15)))
                found = True; i = mss; break
        i += 1
    return out

def trade(s_, arr, entry_kind, tgt_kind):
    t, o, h, l, c = arr; d = s_['d']; e = s_[entry_kind]
    if not np.isfinite(e): return None
    risk = (e - s_['stop']) * d
    if risk <= 0: return None
    if tgt_kind == 'liq':
        if not np.isfinite(s_['liq']): return None
        tp = s_['liq']
        if (tp - e) * d < 2 * risk: return None
    else: tp = e + d * float(tgt_kind[0]) * risk
    m = s_['mss']; L = len(t); fill = None
    for k in range(m + 1, min(L, m + 1 + VALID)):
        if (h[k] >= tp) if d > 0 else (l[k] <= tp): return None          # target reached before the entry: cancelled
        if (l[k] <= e) if d > 0 else (h[k] >= e): fill = k; break
    if fill is None: return None
    for k in range(fill, min(L, fill + HOLD)):
        hs = (l[k] <= s_['stop']) if d > 0 else (h[k] >= s_['stop'])
        ht = k > fill and ((h[k] >= tp) if d > 0 else (l[k] <= tp))
        if hs: return dict(r=-1 - (FM * e + (FT + SLIP) * e) / risk, k=k, fill=fill, why='stop')
        if ht: return dict(r=(tp - e) * d / risk - (FM * e + FM * e) / risk, k=k, fill=fill, why='target')
    k = min(L - 1, fill + HOLD - 1)
    return dict(r=(c[k] - e) * d / risk - (FM * e + (FT + SLIP) * e) / risk, k=k, fill=fill, why='time')

if __name__ == '__main__':
    S = []; data = {}
    for s in SYMS:
        data[s] = load(s); x = setups(s); S += x; print(s, 'setups', len(x), flush=True)
    json.dump(S, open(f'smc_setups_{TF}.json', 'w'), default=float)
    rows = []
    for ek, tk, useb, usek in itertools.product(('fvg', 'ob'), ('2R', '3R', 'liq'), (True, False), (True, False)):
        busy = {}; tr = []
        for s_ in sorted(S, key=lambda z: z['t']):
            if useb and s_['bias'] != s_['d']: continue
            if usek and not s_['kz']: continue
            if busy.get(s_['sym'], -1) >= s_['mss']: continue
            r = trade(s_, data[s_['sym']], ek, tk)
            if r is None: continue
            busy[s_['sym']] = r['k']; tr.append(dict(sym=s_['sym'], t=s_['t'], d=s_['d'], **r))
        name = f"entry={ek} target={tk} bias={'on' if useb else 'off'} killzone={'on' if usek else 'off'}"
        json.dump(tr, open(f'smc{TF}_' + name.replace(' ', '_').replace('=', '-') + '.json', 'w'))
        R = np.array([z['r'] for z in tr]); tt = pd.to_datetime([z['t'] for z in tr], unit='ms')
        h1 = R[tt < pd.Timestamp('2024-01-01')]; h2 = R[tt >= pd.Timestamp('2024-01-01')]
        yp = pd.Series(R, index=tt).groupby(tt.year).sum() if len(R) else pd.Series(dtype=float)
        rows.append((name, len(R), np.mean(R > 0) * 100 if len(R) else 0, R.mean() if len(R) else 0, R.sum(), h1.mean() if len(h1) else np.nan, h2.mean() if len(h2) else np.nan, f"{(yp > 0).sum()}/{len(yp)}"))
    t = pd.DataFrame(rows, columns=['variant', 'n', 'win%', 'avgR', 'sumR', 'avgR 2021-23', 'avgR 2024-26', 'years+'])
    pd.set_option('display.width', 220); print(t.round(3).to_string(index=False))
