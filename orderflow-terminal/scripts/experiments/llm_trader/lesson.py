# The m3 "lesson" as a plain rule (no LLM): momentum + delta into the level -> breakout; slow approach with delta against -> fade.
import run, sys, json, numpy as np, pandas as pd
D = run.load()
part = sys.argv[1]
ev = sorted([e for s in run.SYMS for e in D[s][3]], key=lambda e: e['dec'])
ev = [e for e in ev if (e['dec'] < run.SPLIT) == (part == 'dev') and e['dec'] > pd.Timestamp('2026-07-29')]
res = {'lesson': [], 'lesson_bo_only': []}
for e in ev:
    p, px, a1 = run.picture(D, e); f = dict(run.PICT_FEAT)
    into = f['hi'] * f['r4h'] * 2          # 4h move toward the level, ATR units
    dlt = f['hi'] * f['d30']               # 30 min delta in the approach direction
    kind = 'breakout' if into > 1 and dlt > 0 else 'fade' if into < 0.5 and dlt < 0 else None
    if kind:
        r = run.simulate(D, e, *run.rule(D, e, kind, px, a1)); res['lesson'].append(r)
        if kind == 'breakout': res['lesson_bo_only'].append(r)
for k, xs in res.items():
    R = np.array([x['R'] for x in xs]); rng = np.random.default_rng(0); bs = [rng.choice(R, len(R)).mean() for _ in range(2000)]
    print(part, k, 'trades', len(R), 'win', round(np.mean(R > 0) * 100), '% avgR', round(R.mean(), 3), 'CI90', np.round(np.percentile(bs, [5, 95]), 3))
