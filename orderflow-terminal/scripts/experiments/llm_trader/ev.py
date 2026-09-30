# Summaries: per strategy trades, win rate, avg R, total %, bootstrap 90 % CI of avg R; LLM vs rules on the same events.
import json, sys, numpy as np
def load(p): return [json.loads(l) for l in open(p)]
def stats(xs, name):
    xs = [x for x in xs if x]
    if not xs: return f'{name:10s} trades 0'
    R = np.array([x['R'] for x in xs]); P = np.array([x['pnl'] for x in xs])
    rng = np.random.default_rng(0); bs = [rng.choice(R, len(R)).mean() for _ in range(2000)]
    return f"{name:10s} trades {len(R):4d}  win {np.mean(P > 0) * 100:4.0f}%  avgR {R.mean():+.3f} [{np.percentile(bs, 5):+.3f},{np.percentile(bs, 95):+.3f}]  sum% {P.sum() * 100:+7.2f}  targets {sum(x['why'] == 'target' for x in xs)} stops {sum(x['why'] == 'stop' for x in xs)}"
for p in sys.argv[1:]:
    rs = load(p); print(p, len(rs), 'events')
    for k in ('fade', 'breakout', 'random', 'llm'):
        if k in rs[0]: print(' ', stats([r.get(k) for r in rs], k))
    if 'llm' in rs[0]:
        from collections import Counter
        print('  parse:', dict(Counter(r['parse'] for r in rs)), ' avg secs', round(np.mean([r['secs'] for r in rs]), 1))
        taken = [r for r in rs if r.get('llm')]
        # did the model pick the better side? compare with the rule that traded the same direction
        agree_fade = sum(1 for r in taken if (r['side'] == (-1 if r['level'].endswith('H') else 1)))
        print(f'  of {len(taken)} trades: {agree_fade} fades, {len(taken) - agree_fade} breakouts')
