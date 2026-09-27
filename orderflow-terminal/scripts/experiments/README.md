# Level-engine experiments (Gerchik rules)

Offline, reproducible experiments behind the parameters the site runs (`SITE_GERCHIK_PARAMS`).

Data: one JSON file per coin and time frame in `$DATA` (`SYMBOL_15m.json`, `SYMBOL_1d.json`, rows
`[t, o, h, l, c, v]`), exported from the exchange klines cached in Supabase (`oft.klines`). Not committed.

```bash
B=node_modules/.bin/esbuild
$B scripts/experiments/grid.ts --bundle --platform=node --format=esm --outfile=/tmp/grid.mjs
DATA=/path/to/data SPACE='{"stopAtr":[0.3,0.5],"trend":["strict","none"]}' N=200 OUT=grid.json node /tmp/grid.mjs
python3 scripts/experiments/analyze.py grid.json 100      # TRAIN vs VALIDATION, marginal effects, top configs
$B scripts/experiments/eval.ts --bundle --platform=node --format=esm --outfile=/tmp/eval.mjs
DATA=/path/to/data CFG='{"stopAtr":0.5,"models":"BK"}' node /tmp/eval.mjs   # one config, per coin and segment
```

Every run is bar by bar on closed 15m bars (orders fill only on later bars, stop first when a bar touches
both, Bybit fees in R). Segments: TRAIN = first 50 % of the year, VALIDATION = next 25 %, OOS = last 25 %.

Raschke (Street Smarts) setups: `rasch.ts` (each setup with the book rules; `CFG` overrides, `SETUPS` selects),
`rgrid.ts` (full grid of exits / filters for one setup; `SETUP`, `SPACE`, `OUT`), `analyze_raschke.py`
(TRAIN vs VALIDATION per setup), `combo.ts` (Gerchik + Holy Grail side by side), `site.ts` (the site engine).

Market mechanics and the trend model (daily data, `daily/`): run from a directory that holds `daily/SYMBOL.json`
(daily candles `[t, o, h, l, c, v]`, 2021-2026). `mech.py` — autocorrelation, variance ratios (Lo–MacKinlay),
time-series momentum test, volatility clustering, cross-coin correlation, tails; `dbt.py` — daily backtester
(orders from closed days, stop first, fees, funding estimate) with Holy Grail and Donchian generators, portfolio
metrics, stationary block bootstrap and the deflated Sharpe ratio; `run_fixed.py` — the book / classic rules
without tuning; `wf.py` + `wf_eval.py` — 112 Donchian variants and a rolling walk-forward (train 2 years, trade
the next 6 months). `trend15.ts` checks the 15m TrendEngine against the daily simulator on the same year;
`gd.ts` runs the Gerchik engine on daily bars (5 years); `site.ts` — the site engine on the 15m year.
Research ideas (27.09.2026): `ens.py` — multi-lookback Donchian ensemble with a 25 % volatility target
(Zarattini, Pagani, Barbon 2025); `xs.py` — cross-sectional momentum (Liu, Tsyvinski, Wu); `vt.py`, `vt3.py`, `vt4.py` —
midline exits, portfolio volatility management (Moreira, Muir 2017) with a paired block bootstrap, and a multiplier
fixed at entry. `dbt.py` gained the `('mid', n)` trailing exit and real funding (`fund=`).
Round 2: `r2.py` — volatility squeeze, close/volume confirmation, SMA200 regime, shorts; `fpred.py`, `fpred2.py` —
does funding / open interest predict returns (needs `fund/SYM.json` = [[utc day, daily funding × 1e6]] and
`oi/SYM.json` = [[utc day, open interest]] from Bybit's public API); `ftr.py`, `ftr2.py` — the funding filter on the
trend model and the trades it removes; `site_funding.ts` — the 15m site engine with and without it (`DATA=all`).
