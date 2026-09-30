import pandas as pd, numpy as np
T = pd.read_pickle('fibset/trades.pkl'); T['half'] = np.where(T.t < pd.Timestamp('2023-05-01'), 'A', 'B')
def st(g):
    w = g.R[g.R > 0].sum(); l = -g.R[g.R < 0].sum()
    return pd.Series({'n': len(g), 'win%': (g.R > 0).mean() * 100, 'avgR': g.R.mean(), 'sumR': g.R.sum(), 'PF': w / l if l else np.nan})
main = T[~T.setup.str.startswith('grid')]
print('=== setups from the pictures: all timeframes, both halves ===')
print(main.groupby('setup').apply(st).round(3).to_string())
print('\n=== by timeframe and half: average R per trade ===')
print(main.pivot_table(index='setup', columns=['tf', 'half'], values='R', aggfunc='mean').round(3).to_string())
print('\n=== control: entry level grid (stop beyond the low, target = the high), all TFs ===')
g = T[T.setup.str.startswith('grid')].copy(); g['x'] = g.setup.str[5:].astype(float)
print(g.groupby('x').apply(st).round(3).to_string())
print(g.pivot_table(index='x', columns='tf', values='R', aggfunc='mean').round(3).to_string())
