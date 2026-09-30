# MQL5 article 16957 (robustness testing): trade / time / symbol outlier tests on the site trend model.
import warnings; warnings.filterwarnings('ignore')
from r2 import *
T = []
for s, df in D.items():
    tr, p = simulate(df, gen(df), s, 10000, ('donchian', 10)); T += [t for t in tr if t.t0 >= pd.Timestamp('2021-06-01')]
r = np.array([t.r for t in T]); n = len(r)
print(f"trades {n}, avg R {r.mean():+.3f}, sum R {r.sum():+.0f}, win {np.mean(r > 0) * 100:.0f}%")
o = np.sort(r)[::-1]
for k in (1, 5, 10, 20, int(0.05 * n)):
    rest = o[k:]; print(f"  without best {k:3d} trades: avg R {rest.mean():+.3f}, sum R {rest.sum():+.0f}")
yrs = sorted(set(t.t0.year for t in T))
for y in yrs:
    rest = np.array([t.r for t in T if t.t0.year != y]); print(f"  without {y}: trades {len(rest)}, avg R {rest.mean():+.3f}")
by = {}
for t in T: by.setdefault(t.sym, []).append(t.r)
tot = {k: sum(v) for k, v in by.items()}
top = sorted(tot, key=tot.get, reverse=True)
print('  per coin sum R:', {k: round(tot[k]) for k in top})
for k in (1, 3):
    rest = np.array([t.r for t in T if t.sym not in top[:k]]); print(f"  without top-{k} coins {top[:k]}: avg R {rest.mean():+.3f}, sum {rest.sum():+.0f}")
print('  coins with positive sum R:', sum(v > 0 for v in tot.values()), 'of', len(tot))
