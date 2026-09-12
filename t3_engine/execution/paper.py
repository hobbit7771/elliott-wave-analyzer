"""PAPER execution: simulates fills, slippage and fees without touching a
real exchange. This is the mode the spec requires before TESTNET, and
TESTNET before LIVE (section 24) - it is also what the backtester uses
internally, so a strategy behaves identically whether it's being replayed
against history or run live against simulated fills.
"""

from __future__ import annotations

import time
import uuid
from typing import Dict

from t3_engine.common.models import Order
from t3_engine.common.types import OrderSide, OrderStatus
from t3_engine.execution.base import ExecutionEngine


class PaperExecutionEngine(ExecutionEngine):
    def __init__(self, slippage_bps: float = 2.0, fee_bps: float = 4.0):
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.orders: Dict[str, Order] = {}
        self._seen_client_ids: set = set()

    @staticmethod
    def new_client_order_id() -> str:
        return f"paper-{uuid.uuid4().hex[:16]}"

    def submit_order(self, order: Order, reference_price: float) -> Order:
        if not order.client_order_id:
            order.client_order_id = self.new_client_order_id()

        if order.client_order_id in self._seen_client_ids:
            # Duplicate-order protection (section 24): return the original
            # fill instead of double-executing.
            return self.orders[order.client_order_id]

        slippage = reference_price * (self.slippage_bps / 10_000.0)
        fill_price = reference_price + slippage if order.side == OrderSide.BUY else reference_price - slippage
        fee = fill_price * order.quantity * (self.fee_bps / 10_000.0)

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.avg_fill_price = fill_price
        order.fee = fee
        order.created_at = order.created_at or int(time.time() * 1000)

        self._seen_client_ids.add(order.client_order_id)
        self.orders[order.client_order_id] = order
        return order

    def cancel_order(self, client_order_id: str) -> bool:
        order = self.orders.get(client_order_id)
        if order is None or order.status == OrderStatus.FILLED:
            return False
        order.status = OrderStatus.CANCELED
        return True
