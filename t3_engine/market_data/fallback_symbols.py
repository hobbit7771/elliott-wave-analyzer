"""Static fallback symbol list for the dashboard's symbol picker.

WHY THIS EXISTS: `/api/symbols` normally serves the live list from
Binance's `/fapi/v1/exchangeInfo` (see rest_client.py `list_symbols`), but
that call can fail for reasons entirely outside this app's control - a
regional block (HTTP 451), or an IP-level rate-limit ban (HTTP 418/429,
observed in production on a fresh deploy that had made exactly one prior
Binance request - almost certainly inherited from other tenants sharing
the same outbound IP on Render's shared hosting tiers). When that happens,
leaving the symbol picker empty defeats the entire point of having one -
typing a letter should always suggest *something* real.

THIS LIST IS DELIBERATELY NOT EXHAUSTIVE. It is a hand-picked set of
long-established, high-volume USDT-M perpetual futures symbols chosen for
being extremely unlikely to have been delisted - not a live snapshot, and
not guaranteed byte-for-byte accurate against Binance's current listings
(new symbols launch and some get delisted regularly; verify against
exchangeInfo when you can reach it). `/api/symbols` returns this ONLY as a
fallback, tagged `"source": "fallback"` in the response, and the frontend
replaces it with the real live list transparently the next time a fetch
succeeds - this is a stopgap for "the field must never be empty", not a
claim of completeness.
"""

FALLBACK_USDT_PERPETUAL_SYMBOLS = sorted([
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "TRXUSDT", "TONUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "LINKUSDT", "AVAXUSDT",
    "ATOMUSDT", "UNIUSDT", "ETCUSDT", "XLMUSDT", "NEARUSDT", "FILUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT", "RUNEUSDT", "AAVEUSDT", "MKRUSDT",
    "SANDUSDT", "MANAUSDT", "AXSUSDT", "GALAUSDT", "FTMUSDT", "ALGOUSDT", "VETUSDT",
    "ICPUSDT", "HBARUSDT", "EGLDUSDT", "THETAUSDT", "XTZUSDT", "EOSUSDT", "CHZUSDT",
    "ENJUSDT", "ZECUSDT", "DASHUSDT", "COMPUSDT", "SNXUSDT", "CRVUSDT", "YFIUSDT",
    "BATUSDT", "GRTUSDT", "SUSHIUSDT", "1INCHUSDT", "LRCUSDT", "KSMUSDT", "WAVESUSDT",
    "QTUMUSDT", "ONTUSDT", "IOTAUSDT", "ZILUSDT", "RVNUSDT",
])
