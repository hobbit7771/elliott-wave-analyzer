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
