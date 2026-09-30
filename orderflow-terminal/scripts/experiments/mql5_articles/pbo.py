# MQL5 article 21603 (unified validation: CPCV / V-in-V / CSCV): probability of backtest overfitting (PBO, Bailey et al. 2017)
# for the 112-variant daily breakout family that the site's trend model was chosen from (README round 5).
import pickle, itertools, numpy as np, pandas as pd
cfg, R, _ = pickle.load(open('wf_don.pkl', 'rb'))
R = R[R.index >= '2021-06-01'].fillna(0)
S = 16; blocks = np.array_split(np.arange(len(R)), S); M = R.values
def sr(x): s = x.std(axis=0); return np.where(s > 0, x.mean(axis=0) / s, 0)
logits = []; oos_best = []; is_best = []
for comb in itertools.combinations(range(S), S // 2):
    iis = np.concatenate([blocks[i] for i in comb]); oos = np.concatenate([blocks[i] for i in range(S) if i not in comb])
    a = sr(M[iis]); b = sr(M[oos]); k = int(np.argmax(a))
    rank = (b < b[k]).mean() + 0.5 * (b == b[k]).mean() - 0.5 / len(b)   # relative OOS rank of the IS winner in (0,1)
    rank = min(max(rank, 1e-6), 1 - 1e-6); logits.append(np.log(rank / (1 - rank)))
    oos_best.append(b[k] * np.sqrt(365)); is_best.append(a[k] * np.sqrt(365))
logits = np.array(logits)
print(f"combinations {len(logits)}, PBO = P(IS-best variant falls below the OOS median) = {(logits < 0).mean():.3f}")
print(f"IS-best Sharpe median {np.median(is_best):.2f}; its OOS Sharpe median {np.median(oos_best):.2f}, share of OOS Sharpe > 0: {(np.array(oos_best) > 0).mean():.2f}")
site = [i for i, c in enumerate(cfg) if c == {'n_in': 20, 'n_out': 10, 'stop_atr': 3.0, 'long_only': True, 'btc': True, 'er_min': None}]
full = sr(M) * np.sqrt(365)
print('site variant', site, 'full-period Sharpe', round(full[site[0]], 2) if site else None, '| family median', round(np.median(full), 2), 'share > 0', round((full > 0).mean(), 2))
