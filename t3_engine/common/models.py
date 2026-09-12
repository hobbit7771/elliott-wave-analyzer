"""Core data models shared by every module in the pipeline.

All timestamps are epoch milliseconds (UTC), matching Binance's convention,
so no module needs to do its own timezone math.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

from t3_engine.common.types import (
    Direction,
    EntryStage,
    OrderSide,
    OrderStatus,
    StructureType,
    Timeframe,
    TradeSide,
    WaveLabel,
    WaveStatus,
)

_id_counter = itertools.count(1)


def next_id(prefix: str) -> str:
    return f"{prefix}-{next(_id_counter)}"


@dataclass
class Candle:
    """One OHLCV bar. `closed=False` marks the currently-forming bar of a
    live aggregation - strategy code must never treat an open candle as a
    confirmed pivot input (that would be lookahead in disguise)."""

    timeframe: Timeframe
    open_time: int  # ms, inclusive
    close_time: int  # ms, inclusive
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    taker_buy_volume: float = 0.0
    trades: int = 0
    closed: bool = True

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def taker_sell_volume(self) -> float:
        return max(self.volume - self.taker_buy_volume, 0.0)


@dataclass
class Pivot:
    """A confirmed swing high/low used as raw material for wave counting."""

    index: int  # position in the causal candle series it was detected on
    timestamp: int
    price: float
    kind: str  # "HIGH" or "LOW"
    confirmed_at_index: int  # index of the candle that CONFIRMED this pivot (>= index)


@dataclass
class Wave:
    """One labelled Elliott wave leg. Mirrors section 4 of the spec exactly."""

    wave_id: str
    parent_wave_id: Optional[str]
    degree: Timeframe
    label: WaveLabel
    direction: Direction
    start_timestamp: int
    end_timestamp: int
    start_price: float
    end_price: float
    high: float
    low: float
    status: WaveStatus = WaveStatus.CANDIDATE
    confidence: float = 0.0
    invalid_level: Optional[float] = None
    fib_relationships: dict = field(default_factory=dict)
    subwave_count: int = 0
    structure_type: Optional[StructureType] = None

    @property
    def length(self) -> float:
        return abs(self.end_price - self.start_price)


@dataclass
class Scenario:
    """One candidate Elliott count. The engine keeps the top-3 alive
    simultaneously (section 7)."""

    scenario_id: str
    degree: Timeframe
    waves: list = field(default_factory=list)  # list[Wave], oldest->newest
    probability: float = 0.0
    elliott_validity: float = 0.0
    fib_score: float = 0.0
    price_action_score: float = 0.0
    volume_score: float = 0.0
    momentum_score: float = 0.0
    microstructure_score: float = 0.0
    derivatives_score: float = 0.0
    invalidation: Optional[float] = None
    expected_target: Optional[float] = None
    status: WaveStatus = WaveStatus.DEVELOPING
    next_expected_label: Optional[WaveLabel] = None

    @property
    def current_wave(self) -> Optional[Wave]:
        return self.waves[-1] if self.waves else None


@dataclass
class Signal:
    """Full audit record for one entry decision - accepted or rejected.
    Every field in section 17/23 is captured so backtests are reproducible
    and honest (section 16: no-repaint / no-lookahead)."""

    signal_id: str
    symbol: str
    created_at: int
    data_available_at_signal: int
    side: TradeSide
    wave_label: WaveLabel
    entry_stage: EntryStage
    confidence: float
    score_breakdown: dict
    entry_zone: tuple
    stop_loss: float
    take_profits: list
    risk_reward: float
    invalidation: float
    wave_state_at_signal: dict
    reason: str
    decision: str  # SignalDecision value
    scenario_id: Optional[str] = None
    rejection_reason: Optional[str] = None


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    order_type: str = "MARKET"
    price: Optional[float] = None
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: float = 0.0
    avg_fill_price: Optional[float] = None
    fee: float = 0.0
    created_at: int = 0
    signal_id: Optional[str] = None
    tag: Optional[str] = None  # e.g. "ENTRY", "TP1", "SL"


@dataclass
class TakeProfitLeg:
    price: float
    fraction: float  # fraction of remaining position to close, 0..1
    label: str
    filled: bool = False


@dataclass
class Position:
    position_id: str
    symbol: str
    side: TradeSide
    entry_price: float
    quantity: float
    initial_quantity: float
    stop_loss: float
    take_profits: list  # list[TakeProfitLeg]
    opened_at: int
    wave_label: WaveLabel
    signal_id: str
    risk_amount: float
    realized_pnl: float = 0.0
    closed: bool = False
    closed_at: Optional[int] = None
    mae: float = 0.0  # max adverse excursion (price units, positive)
    mfe: float = 0.0  # max favorable excursion (price units, positive)
    trailing_stop_active: bool = False
