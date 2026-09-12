"""Position lifecycle: open, track MAE/MFE, partial TP fills, structural
trailing stop, and close (spec sections 14-15).

Key rule enforced here mechanically (not just by convention): the stop can
only ever move in the risk-reducing direction (`validate_stop_not_widened`
from risk_engine). `update_trailing_stop` raises rather than silently
accepting a widened stop.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from t3_engine.common.models import Order, Position, Signal, TakeProfitLeg, next_id
from t3_engine.common.types import OrderSide, TradeSide
from t3_engine.execution.base import ExecutionEngine
from t3_engine.risk_engine.risk_manager import RiskManager, validate_stop_not_widened
from t3_engine.common.types import Direction


class StopWideningRejected(Exception):
    pass


class PositionManager:
    def __init__(self, execution: ExecutionEngine, risk_manager: RiskManager):
        self.execution = execution
        self.risk_manager = risk_manager
        self.positions: Dict[str, Position] = {}
        self.closed_positions: List[Position] = []

    def open_position(self, *, signal: Signal, symbol: str, side: TradeSide, entry_price: float,
                       quantity: float, stop_loss: float, take_profits: List[TakeProfitLeg],
                       wave_label, now_ms: Optional[int] = None) -> Position:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        order_side = OrderSide.BUY if side == TradeSide.LONG else OrderSide.SELL
        order = Order(client_order_id=next_id("order"), symbol=symbol, side=order_side,
                       quantity=quantity, order_type="MARKET", signal_id=signal.signal_id, tag="ENTRY")
        filled = self.execution.submit_order(order, reference_price=entry_price)

        risk_amount = self.risk_manager.risk_amount_for(wave_label)
        position = Position(
            position_id=next_id("position"), symbol=symbol, side=side,
            entry_price=filled.avg_fill_price, quantity=quantity, initial_quantity=quantity,
            stop_loss=stop_loss, take_profits=list(take_profits), opened_at=now_ms,
            wave_label=wave_label, signal_id=signal.signal_id, risk_amount=risk_amount,
        )
        self.positions[position.position_id] = position
        self.risk_manager.register_trade_opened(position.position_id, wave_label, risk_amount)
        return position

    def update_trailing_stop(self, position_id: str, new_stop: float) -> None:
        pos = self.positions[position_id]
        direction = Direction.UP if pos.side == TradeSide.LONG else Direction.DOWN
        if not validate_stop_not_widened(pos.stop_loss, new_stop, direction):
            raise StopWideningRejected(
                f"Refusing to widen stop for {position_id}: {pos.stop_loss} -> {new_stop} "
                f"(section 14: never widen a stop to save a trade)"
            )
        pos.stop_loss = new_stop
        pos.trailing_stop_active = True

    def on_price_update(self, position_id: str, price: float, now_ms: int) -> Optional[str]:
        """Feed the latest price for an open position. Updates MAE/MFE,
        checks stop-loss and take-profit legs. Returns a short reason
        string if the position was closed (fully or partially) this call,
        else None."""
        pos = self.positions.get(position_id)
        if pos is None or pos.closed:
            return None

        # MAE/MFE track the actual adverse/favorable EXCURSION within the
        # bar - this must stay the raw wick extreme (`price`), unlike the
        # fill prices below, since its whole purpose is showing how far
        # price actually moved against/for the trade.
        favorable = (price - pos.entry_price) if pos.side == TradeSide.LONG else (pos.entry_price - price)
        pos.mfe = max(pos.mfe, favorable)
        pos.mae = max(pos.mae, -favorable)

        stop_hit = (price <= pos.stop_loss) if pos.side == TradeSide.LONG else (price >= pos.stop_loss)
        if stop_hit:
            # Fill AT the stop level, not at whatever wick extreme
            # (candle.low/.high) happened to trigger it. The caller only
            # ever passes a bar's full high/low - on a wide bar (routine
            # on 1h/4h, which is exactly what surfaced this: R multiples
            # of -50 or worse on trades whose intended risk was ~1%) that
            # extreme can be dramatically further from entry than the
            # actual stop distance the position was sized against, which
            # silently turned every stop-out into a much larger loss than
            # the risk engine ever intended - not "the market was against
            # us", a bug. This is the standard, conservative backtesting
            # assumption (a stop order fills at its trigger price) absent
            # real tick-level gap data to model slippage through it.
            self._close(pos, pos.stop_loss, now_ms, "STOP_LOSS")
            return "STOP_LOSS"

        for tp in pos.take_profits:
            if tp.filled:
                continue
            tp_hit = (price >= tp.price) if pos.side == TradeSide.LONG else (price <= tp.price)
            if tp_hit:
                # Same reasoning as the stop fill above, symmetrically: fill
                # AT the take-profit level, not at a wick that happened to
                # run further - otherwise wins get overstated by the exact
                # same bug that was overstating losses on stops.
                self._partial_close(pos, tp, tp.price, now_ms)
                return f"TP_HIT:{tp.label}"

        return None

    def _partial_close(self, pos: Position, tp: TakeProfitLeg, price: float, now_ms: int) -> None:
        close_qty = pos.initial_quantity * tp.fraction
        close_qty = min(close_qty, pos.quantity)
        order_side = OrderSide.SELL if pos.side == TradeSide.LONG else OrderSide.BUY
        order = Order(client_order_id=next_id("order"), symbol=pos.symbol, side=order_side,
                      quantity=close_qty, reduce_only=True, signal_id=pos.signal_id, tag=tp.label)
        filled = self.execution.submit_order(order, reference_price=price)

        pnl = (filled.avg_fill_price - pos.entry_price) * close_qty if pos.side == TradeSide.LONG \
            else (pos.entry_price - filled.avg_fill_price) * close_qty
        pnl -= filled.fee
        pos.realized_pnl += pnl
        pos.quantity -= close_qty
        tp.filled = True

        if pos.quantity <= 1e-12:
            pos.closed = True
            pos.closed_at = now_ms
            self.risk_manager.register_trade_closed(pos.position_id, pos.realized_pnl)
            self.closed_positions.append(pos)

    def _close(self, pos: Position, price: float, now_ms: int, reason: str) -> None:
        order_side = OrderSide.SELL if pos.side == TradeSide.LONG else OrderSide.BUY
        order = Order(client_order_id=next_id("order"), symbol=pos.symbol, side=order_side,
                      quantity=pos.quantity, reduce_only=True, signal_id=pos.signal_id, tag=reason)
        filled = self.execution.submit_order(order, reference_price=price)

        pnl = (filled.avg_fill_price - pos.entry_price) * pos.quantity if pos.side == TradeSide.LONG \
            else (pos.entry_price - filled.avg_fill_price) * pos.quantity
        pnl -= filled.fee
        pos.realized_pnl += pnl
        pos.quantity = 0.0
        pos.closed = True
        pos.closed_at = now_ms
        self.risk_manager.register_trade_closed(pos.position_id, pos.realized_pnl)
        self.closed_positions.append(pos)
