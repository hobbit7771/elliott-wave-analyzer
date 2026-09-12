"""Execution engine interface (spec section 24).

Every mode (PAPER, TESTNET, LIVE) implements this same interface so the
rest of the pipeline (position_manager, backtester) never needs to know
which one it's talking to. `submit_order` must always be idempotent under
retried calls with the same `client_order_id` (duplicate-order protection,
section 24).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from t3_engine.common.models import Order


class ExecutionEngine(ABC):
    @abstractmethod
    def submit_order(self, order: Order, reference_price: float) -> Order:
        """Submit (and, for PAPER/backtest, immediately simulate the fill
        of) an order. Returns the order with fill fields populated."""

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> bool:
        ...
