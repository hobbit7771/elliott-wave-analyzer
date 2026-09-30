# Memory vote without the LLM: top-K most similar earlier touches (outcome known), trade the approach that won more often.
import run, sys, numpy as np, pandas as pd
from collections import Counter
D = run.load()
allev = sorted([e for s in run.SYMS for e in D[s][3]], key=lambda e: e['dec'])
rows = []
for e in allev:
    if e['dec'] <= pd.Timestamp('2026-07-29'): continue
    p, px, a1 = run.picture(D, e); f = dict(run.PICT_FEAT)
    fr = run.simulate(D, e, *run.rule(D, e, 'fade', px, a1)); br = run.simulate(D, e, *run.rule(D, e, 'breakout', px, a1))
    f['_win'] = 'fade' if fr['why'] == 'target' else 'breakout' if br['why'] == 'target' else 'neither'
    rows.append((e, f, fr, br))
def run_k(K, margin, part):
    out = []
    for i, (e, f, fr, br) in enumerate(rows):
        if (e['dec'] < run.SPLIT) != (part == 'dev'): continue
        mem = [r for r in rows[:i] if r[0]['dec'] + pd.Timedelta(hours=4) <= e['dec']]
        if len(mem) < K: continue
        keys = [k for k in f if not k.startswith('_')]
        d = [sum((f[k] - m[1][k]) ** 2 for k in keys) for m in mem]
        c = Counter(mem[j][1]['_win'] for j in np.argsort(d)[:K])
        if c['fade'] - c['breakout'] >= margin: out.append(fr)
        elif c['breakout'] - c['fade'] >= margin: out.append(br)
    R = np.array([x['R'] for x in out])
    return len(R), R.mean() if len(R) else np.nan
part = sys.argv[1] if len(sys.argv) > 1 else 'dev'
for K in (5, 10, 20):
    for mg in (1, 2, 3):
        n, r = run_k(K, mg, part); print(part, 'K', K, 'margin', mg, 'trades', n, 'avgR', round(r, 3))
