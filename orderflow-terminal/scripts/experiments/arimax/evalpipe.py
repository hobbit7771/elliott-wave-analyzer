# Layer-by-layer evaluation of the user's pipeline (rules fixed before the run, see pipe.py).
# ML layer: LightGBM classifier retrained monthly on all earlier candles (5-candle embargo), from 2023-01;
# target = the 5-candle trade beats the round-trip cost in the trade's direction.
import sys, glob, numpy as np, pandas as pd, lightgbm as lgb, warnings
warnings.filterwarnings('ignore')
S = '/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad'
TF = sys.argv[1]; H = 5; COST = 0.15
D = pd.concat([pd.read_pickle(p) for p in sorted(glob.glob(f'{S}/arimax/{TF}_*.pkl'))])
D.index.name = 't'; D = D.reset_index()
btc = D[D.sym == 'BTCUSDT'].set_index('t').c.resample('D').last()
reg = (btc > btc.rolling(50).mean()).shift(1)                          # yesterday's close vs its 50-day SMA
D['bull'] = D.t.dt.floor('D').map(reg).astype(float)
for k in range(1, 6): D[f'r{k}'] = D.groupby('sym').r.shift(k - 1)
D['hour'] = D.t.dt.hour
FEAT = ['F', 'W', 'cvd5', 'oi5', 'fund', 'volx', 'dsup', 'dres', 'r1', 'r2', 'r3', 'r4', 'r5', 'hour']
D = D.replace([np.inf, -np.inf], np.nan)
# ---- ML classifier, walk-forward ----
D['pL'] = np.nan; D['pS'] = np.nan
months = pd.date_range('2023-01-01', D.t.max(), freq='MS')
prm = dict(objective='binary', learning_rate=0.05, num_leaves=15, min_data_in_leaf=200, feature_fraction=0.8, bagging_fraction=0.7, bagging_freq=1, verbose=-1, seed=1, num_threads=6)
emb = pd.Timedelta(hours=H * (1 if TF == '1h' else 4))
for m in months:
    tr = D[(D.t < m - emb) & D.fwd.notna() & D.F.notna()]; te = (D.t >= m) & (D.t < m + pd.offsets.MonthBegin(1)) & D.F.notna()
    if not te.any(): continue
    for side, col in ((1, 'pL'), (-1, 'pS')):
        mdl = lgb.train(prm, lgb.Dataset(tr[FEAT], (side * tr.fwd > COST).astype(int)), num_boost_round=200)
        D.loc[te, col] = mdl.predict(D.loc[te, FEAT])
D['half'] = np.where(D.t < '2024-03-01', 'A (2022-03..2024-02)', 'B (2024-03..2026-08)')
lv = np.log(1.2)
def layers(side):
    s = side
    F = D.F * s > 0.5
    lvl = (D.dsup.between(0, 1)) if s == 1 else (D.dres.between(0, 1))
    cvd = D.cvd5 * s > 0; vol = D.volx > lv; oi = D.oi5 > 0; fund = D.fund.abs() <= 0.0002
    ml = (D.pL if s == 1 else D.pS) > 0.55; regime = (D.bull == 1) if s == 1 else (D.bull == 0)
    L = [('0 без фильтров (каждая свеча)', pd.Series(True, index=D.index)), ('1 ARIMAX > 0,5 %', F), ('2 + сильный уровень', F & lvl), ('3 + CVD', F & lvl & cvd),
         ('4 + объём', F & lvl & cvd & vol), ('5 + OI растёт', F & lvl & cvd & vol & oi), ('6 + фандинг нейтрален', F & lvl & cvd & vol & oi & fund),
         ('7 + ML > 0,55', F & lvl & cvd & vol & oi & fund & ml), ('8 + режим рынка', F & lvl & cvd & vol & oi & fund & ml & regime),
         ('контроль: правила 2–6 + режим без ARIMAX', lvl & cvd & vol & oi & fund & regime), ('контроль: только ML > 0,55', ml)]
    return L
def nonoverlap(mask):
    keep = np.zeros(len(D), bool)
    for sym, idx in D[mask & D.fwd.notna()].groupby('sym').groups.items():
        last = -10 ** 9
        for i in sorted(idx):
            if i - last >= H: keep[i] = True; last = i                     # rows of one coin are consecutive candles
    return keep
rows = []
for side, name in ((1, 'LONG'), (-1, 'SHORT')):
    for lab, m in layers(side):
        k = nonoverlap(m.fillna(False).values & D.F.notna().values)
        net = side * D.fwd[k] - COST
        for h, g in net.groupby(D.half[k]): rows.append((name, lab, h, len(g), g.mean(), (g > 0).mean() * 100, g.sum()))
R = pd.DataFrame(rows, columns=['side', 'layer', 'half', 'n', 'avg %', 'win %', 'sum %'])
pd.set_option('display.width', 250)
print(TF, '— net of 0.15 % round trip, 5-candle hold, non-overlapping per coin')
print(R.pivot_table(index=['side', 'layer'], columns='half', values=['n', 'avg %', 'win %'], sort=False).round(3).to_string())
x = D.dropna(subset=['F', 'fwd'])
print('\nARIMAX skill, all coins:', 'corr(F, realised) %.4f' % x.F.corr(x.fwd), '| direction hit %.3f' % (np.sign(x.F) == np.sign(x.fwd)).mean(),
      '| OOS R2 vs zero %.4f' % (1 - ((x.fwd - x.F) ** 2).sum() / (x.fwd ** 2).sum()), '| median 95%% half-width %.2f %%' % x.W.median(),
      '| share |F| > 0.5 %%: %.3f' % (x.F.abs() > 0.5).mean())
y = D.dropna(subset=['pL', 'fwd']); from sklearn.metrics import roc_auc_score
print('ML AUC long %.3f short %.3f' % (roc_auc_score(y.fwd > COST, y.pL), roc_auc_score(y.fwd < -COST, y.pS)))
R.to_pickle(f'{S}/arimax/layers_{TF}.pkl')
