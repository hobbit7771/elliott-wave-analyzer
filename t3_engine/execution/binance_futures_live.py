"""TESTNET / LIVE execution scaffold for Binance USDT-M Futures.

WHY THIS IS A SCAFFOLD, NOT A CERTIFIED IMPLEMENTATION (per spec section
30 - explain the technical reason and give the nearest correct path when a
requirement can't be delivered literally):

This engine was built inside a sandboxed session whose outbound network
policy blocks `fapi.binance.com` (confirmed: the proxy returns a policy
403 on CONNECT). That means signed REST order calls and the user-data
WebSocket cannot be exercised or validated against a real Binance account
from here - not even against Testnet, which still requires real outbound
HTTPS to `testnet.binancefuture.com`.

Shipping a "LIVE" class that has never actually completed a signed
request against the exchange, and calling it done, would be dishonest and
dangerous with real money. Section 24 also explicitly requires a staged
PAPER -> TESTNET -> LIVE rollout with statistical validation in between -
skipping straight to a LIVE class satisfies neither the letter nor the
spirit of that requirement.

What IS implemented: the full `ExecutionEngine` interface is honoured, the
HMAC-SHA256 request-signing helper (the actual mechanism Binance requires)
is real and unit-testable without network access, and the order payload
builder matches the documented `/fapi/v1/order` schema (symbol, side,
type, quantity, reduceOnly, newClientOrderId). Wiring `submit_order` to
actually call `httpx.post` is a ~10 line change once you have API keys and
network access to test with - the TODO below marks exactly where.

To go live yourself:
  1. Set T3_BINANCE_API_KEY / T3_BINANCE_API_SECRET (Testnet keys first).
  2. Point `base_url` at https://testnet.binancefuture.com.
  3. Uncomment the network call in `submit_order`, run the paper->testnet
     comparison in backtest/ to confirm fills behave as expected.
  4. Only after a real Testnet track record should `base_url` move to
     https://fapi.binance.com and `TradingMode.LIVE` be enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Dict

from t3_engine.common.models import Order
from t3_engine.execution.base import ExecutionEngine


def sign_payload(secret: str, params: Dict[str, str]) -> str:
    """Binance's documented request-signing scheme: HMAC-SHA256 over the
    exact query string that will be sent, hex digest appended as
    `signature`. Pure function, fully testable offline."""
    query_string = urllib.parse.urlencode(params)
    return hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()


def build_order_params(order: Order, position_side: str = "BOTH") -> Dict[str, str]:
    """Builds the exact param dict /fapi/v1/order expects. Kept separate
    from any network call so it can be tested without one."""
    params = {
        "symbol": order.symbol,
        "side": order.side.value,
        "type": order.order_type,
        "quantity": f"{order.quantity}",
        "newClientOrderId": order.client_order_id,
        "reduceOnly": "true" if order.reduce_only else "false",
        "timestamp": str(int(time.time() * 1000)),
    }
    if order.order_type != "MARKET" and order.price is not None:
        params["price"] = f"{order.price}"
        params["timeInForce"] = "GTC"
    return params


class BinanceFuturesLiveExecutionEngine(ExecutionEngine):
    """NOT certified for real trading in this session - see module
    docstring. Raises NotImplementedError on submit_order until the
    network call is wired up and tested with real Testnet credentials."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://testnet.binancefuture.com"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def submit_order(self, order: Order, reference_price: float) -> Order:
        params = build_order_params(order)
        params["signature"] = sign_payload(self.api_secret, params)
        # TODO (requires network + real keys, see module docstring):
        #   resp = httpx.post(f"{self.base_url}/fapi/v1/order", params=params,
        #                      headers={"X-MBX-APIKEY": self.api_key}, timeout=10)
        #   resp.raise_for_status()
        #   ... parse resp.json() into `order.status/avg_fill_price/...`
        raise NotImplementedError(
            "Live/Testnet order submission requires real API credentials and outbound "
            "network access to Binance, neither of which are available in this sandboxed "
            "build session. The signing + payload-building logic above is real and tested; "
            "only the final network call is stubbed. See module docstring for the exact "
            "3-step activation path."
        )

    def cancel_order(self, client_order_id: str) -> bool:
        raise NotImplementedError("See submit_order - same network limitation applies.")
