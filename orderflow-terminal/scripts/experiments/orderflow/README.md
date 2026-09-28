# Order-flow scalping study (Binance USDⓈ-M public archive)

Data: `data.binance.vision` (reachable through its S3 endpoint `s3-ap-northeast-1.amazonaws.com/data.binance.vision`):
daily `aggTrades` (every trade with the aggressor side) and `bookDepth` (cumulative depth at ±1…5 %, every 30 s).
Nothing is committed; the scripts rebuild it.

1. `fetch.py SYMS N [END]` — downloads N days ending at END and aggregates to 1-second bars
   (buy/sell volume by aggressor, counts, OHLC, VWAP, largest trade) + depth snapshots → `$SEC/SYM_DAY.npz`.
2. `feat.py` (env `SEC`, `STEP`, `HZ`, `OUT`) — features known at second t (trade-flow imbalance 1 s…5 min, intensity,
   large trades, returns in volatility units, realized volatility, range position, depth imbalance ±1/2/5 %, BTC lead
   features for ETH/SOL, time of day) and forward returns: `y{h}` from t, `e{h}` executable (enter 1 s later).
3. `sm.py H [TRAIN_DAYS]` (env `FEAT`, `PRED`) — Ridge + logistic + LightGBM and their ensemble, trained on the previous
   10 days (rows whose target reaches the test day purged), tested on the next day; IC and a trade simulation.
4. `strict.py` — thresholds from past days only + bootstrap confidence intervals; `oos.py` — the pre-registered check.
