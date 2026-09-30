import json, sys, numpy as np, pandas as pd
def summ(f, label=''):
    d = pd.DataFrame(json.load(open(f)))
    if d.empty: return print(f'{label:34s} no trades')
    d['m'] = d.model.map(lambda x: 'TREND' if 'TREND' in str(x) else x)
    def st(x):
        r = x.r.values; pf = r[r > 0].sum() / -r[r < 0].sum() if (r < 0).any() else np.inf
        return f"n {len(r):4d} win {np.mean(r > 0) * 100:3.0f}% avg {r.mean():+.3f} sum {r.sum():+7.1f} PF {pf:4.2f}"
    s = f'{label:34s} ALL {st(d)}'
    for m, g in d.groupby('m'): s += f' | {m} {st(g)}'
    print(s)
    return d
if __name__ == '__main__':
    for f in sys.argv[1:]: summ(f, f)
