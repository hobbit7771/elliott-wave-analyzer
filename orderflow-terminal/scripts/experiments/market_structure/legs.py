# Trend legs between ZigZag swing points (k daily ATRs): does the chance of a trend ending rise with its age / size
# (exhaustion, time cycles), and are consecutive legs proportional (wave ratios)?
import sys, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
K = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
rows = []
for s in members():
    d = load(s); p = zigzag(d, K); a = (atr(d) * np.sqrt(24)).values; inu = d.inu.values; fund = d.fund.values
    for j in range(1, len(p)):
        x0, x1 = p[j - 1], p[j]
        if not inu[x1[0]]: continue
        rows.append(dict(sym=s, t=d.index[x1[0]], dir=-x0[3], hours=x1[0] - x0[0], size=abs(x1[2] - x0[2]) / a[x1[0]]))
L = pd.DataFrame(rows).dropna(); L['days'] = L.hours / 24
print(f"K={K}: legs {len(L)}; median duration {L.days.median():.1f} days, median size {L['size'].median():.2f} daily ATRs; up legs {np.mean(L.dir > 0) * 100:.0f}%")
# hazard: probability that a leg ends during day d given it lasted d days so far
ages = np.arange(0, 30)
haz = [(np.sum((L.days >= a) & (L.days < a + 1)) / max(np.sum(L.days >= a), 1)) for a in ages]
print('P(leg ends on day d | still running at d), d = 0..14:', ' '.join(f"{h * 100:.0f}" for h in haz[:15]))
szb = np.arange(K, K + 12, 1.0)
hs = [np.sum((L['size'] >= b) & (L['size'] < b + 1)) / max(np.sum(L['size'] >= b), 1) for b in szb]
print('P(leg ends within the next 1 ATR | already moved s ATRs), s =', ' '.join(f"{b:.0f}:{h * 100:.0f}%" for b, h in zip(szb, hs)))
# consecutive legs in the same coin: ratio of a leg to the previous leg of the same direction and of the opposite direction
L = L.sort_values(['sym', 't']); L['prev_size'] = L.groupby('sym')['size'].shift(1); L['prev2'] = L.groupby('sym')['size'].shift(2)
print('corr(size, previous opposite leg) %.2f | corr(size, previous same-direction leg) %.2f' % (L['size'].corr(L.prev_size), L['size'].corr(L.prev2)))
r = (L['size'] / L.prev2).dropna(); r = r[(r > 0.2) & (r < 5)]
bins = np.arange(0.2, 3.01, 0.05); h, _ = np.histogram(r, bins); sm = pd.Series(h).rolling(7, center=True, min_periods=1).mean().values
for f in (0.618, 1.0, 1.618, 2.618):
    k = int((f - 0.2) / 0.05)
    if k < len(h): print(f"  same-direction leg ratio near {f}: {h[k]} vs smooth {sm[k]:.0f} ({(h[k] / sm[k] - 1) * 100:+.0f}%)")
# Gann time: any excess of leg durations at 'cycle' lengths (in days) vs smooth background
dh, _ = np.histogram(L.days, np.arange(0, 60, 1)); dsm = pd.Series(dh).rolling(5, center=True, min_periods=1).mean().values
print('  duration histogram excess at 7/14/21/30 days:', ' '.join(f"{x}d {(dh[x] / dsm[x] - 1) * 100:+.0f}%" for x in (7, 14, 21, 30) if x < len(dh)))
