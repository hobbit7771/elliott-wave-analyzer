# 1) Fibonacci: retracement depth of swing B relative to impulse A — does it cluster at 0.382 / 0.5 / 0.618 / 0.786?
# 2) Dow theory: after a higher high AND a higher low, how often is the next high higher? (vs all swings)
import sys, numpy as np, pandas as pd
sys.path.insert(0, '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad/mkt')
from common import *
K = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
ratios = []; dow = []
for s in members():
    d = load(s); p = zigzag(d, K)
    inu = d.inu.values
    for j in range(2, len(p)):
        a, b, c = p[j - 2], p[j - 1], p[j]
        if not inu[c[0]]: continue
        imp = abs(b[2] - a[2]); ret = abs(c[2] - b[2])
        if imp > 0: ratios.append(ret / imp)
    for j in range(4, len(p)):
        seq = p[j - 4:j + 1]
        if not inu[seq[-1][0]]: continue
        # last element seq[-1]; compare same-type pivots
        x0, x1, x2, x3, x4 = seq
        if x4[3] == 1:   # high now; previous high x2, lows x1 (older) x3 (newer)
            up_struct = x2[2] > x0[2] and x3[2] > x1[2]      # prior HH and HL
            dn_struct = x2[2] < x0[2] and x3[2] < x1[2]
            dow.append(('high', up_struct, dn_struct, x4[2] > x2[2]))
        else:
            up_struct = x3[2] > x1[2] and x2[2] > x0[2]
            dn_struct = x3[2] < x1[2] and x2[2] < x0[2]
            dow.append(('low', up_struct, dn_struct, x4[2] < x2[2]))
r = np.array(ratios); r = r[(r > 0.05) & (r < 1.0)]    # retracements that did not exceed the impulse
print(f"K={K} daily ATRs: swings used {len(r)} retracements < 100 %")
bins = np.arange(0.05, 1.0001, 0.01); hcount, _ = np.histogram(r, bins)
sm = pd.Series(hcount).rolling(9, center=True, min_periods=1).mean().values   # smooth background
for f in (0.236, 0.382, 0.5, 0.618, 0.786):
    k = int((f - 0.05) / 0.01)
    print(f"  level {f:.3f}: count in bin {hcount[k]:5d}, smooth background {sm[k]:7.1f}, excess {(hcount[k] / sm[k] - 1) * 100:+5.1f}%")
q = np.percentile(r, [10, 25, 50, 75, 90]); print('  retracement percentiles 10/25/50/75/90:', np.round(q, 3))
D = pd.DataFrame(dow, columns=['kind', 'up', 'dn', 'cont'])
for kind, lab in (('high', 'next high above previous high'), ('low', 'next low below previous low')):
    x = D[D.kind == kind]
    print(f"  {lab}: all swings {x.cont.mean() * 100:.1f}% | after HH+HL (uptrend) {x[x.up].cont.mean() * 100:.1f}% (n={x.up.sum()}) | after LH+LL (downtrend) {x[x.dn].cont.mean() * 100:.1f}% (n={x.dn.sum()})")
# significance: is the excess at the Fibonacci bins larger than at arbitrary bins? (all bins' excess distribution)
ex = hcount / sm - 1; inner = ex[5:-5]
fibk = [int((f - 0.05) / 0.01) for f in (0.382, 0.5, 0.618)]
print(f"  mean excess at 0.382/0.5/0.618: {np.mean(ex[fibk]) * 100:+.1f}% ; share of all bins with excess >= that: {np.mean(inner >= np.mean(ex[fibk])) * 100:.0f}% ; std of bin excess {inner.std() * 100:.1f}%")
# finer test with ±0.5 % windows (exact level touches)
for f in (0.382, 0.5, 0.618):
    near = np.mean(np.abs(r - f) < 0.005); ring = np.mean((np.abs(r - f) >= 0.005) & (np.abs(r - f) < 0.03)) / 5
    print(f"  within ±0.5 % of {f}: {near * 100:.2f}% of swings vs neighbours {ring * 100:.2f}% per same width")
