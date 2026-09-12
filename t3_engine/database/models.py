"""SQLAlchemy ORM models (spec section 26).

Default `database_url` is SQLite (`sqlite:///./t3_engine.db`) so the
project runs with zero external setup. For production, point
T3_DATABASE_URL at Postgres/TimescaleDB - `schema.sql` alongside this file
has the hypertable DDL for the time-series-heavy tables (raw_trades,
candles), which SQLite has no equivalent for (that is the one thing this
ORM layer intentionally does NOT try to abstract away - hypertable
conversion is a one-time `SELECT create_hypertable(...)` you run yourself
against Postgres, see schema.sql).
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class RawTradeRow(Base):
    __tablename__ = "raw_trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False, index=True)
    trade_id = Column(String, nullable=False)
    timestamp = Column(Integer, nullable=False, index=True)
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    is_buyer_maker = Column(Boolean, nullable=False)


class CandleRow(Base):
    __tablename__ = "candles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False, index=True)
    timeframe = Column(String, nullable=False, index=True)
    open_time = Column(Integer, nullable=False, index=True)
    close_time = Column(Integer, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    taker_buy_volume = Column(Float, nullable=False, default=0.0)
    trades = Column(Integer, nullable=False, default=0)


class WaveStateRow(Base):
    __tablename__ = "wave_states"
    id = Column(Integer, primary_key=True, autoincrement=True)
    wave_id = Column(String, nullable=False, unique=True, index=True)
    parent_wave_id = Column(String, nullable=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    degree = Column(String, nullable=False)
    label = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    start_timestamp = Column(Integer, nullable=False)
    end_timestamp = Column(Integer, nullable=False)
    start_price = Column(Float, nullable=False)
    end_price = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    invalid_level = Column(Float, nullable=True)
    fib_relationships = Column(JSON, nullable=True)
    subwave_count = Column(Integer, nullable=False, default=0)
    structure_type = Column(String, nullable=True)


class WaveScenarioRow(Base):
    __tablename__ = "wave_scenarios"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scenario_id = Column(String, nullable=False, unique=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    degree = Column(String, nullable=False)
    probability = Column(Float, nullable=False)
    elliott_validity = Column(Float, nullable=False)
    fib_score = Column(Float, nullable=False)
    price_action_score = Column(Float, nullable=False)
    volume_score = Column(Float, nullable=False)
    momentum_score = Column(Float, nullable=False)
    microstructure_score = Column(Float, nullable=False)
    derivatives_score = Column(Float, nullable=False)
    invalidation = Column(Float, nullable=True)
    expected_target = Column(Float, nullable=True)
    status = Column(String, nullable=False)
    wave_ids = Column(JSON, nullable=True)  # ordered list of wave_id referencing wave_states
    created_at = Column(Integer, nullable=False)


class SignalRow(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(String, nullable=False, unique=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    created_at = Column(Integer, nullable=False, index=True)
    data_available_at_signal = Column(Integer, nullable=False)
    side = Column(String, nullable=False)
    wave_label = Column(String, nullable=False)
    entry_stage = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    score_breakdown = Column(JSON, nullable=True)
    entry_zone_low = Column(Float, nullable=True)
    entry_zone_high = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=False)
    take_profits = Column(JSON, nullable=True)
    risk_reward = Column(Float, nullable=False)
    invalidation = Column(Float, nullable=False)
    wave_state_at_signal = Column(JSON, nullable=True)
    reason = Column(String, nullable=True)
    decision = Column(String, nullable=False)
    scenario_id = Column(String, nullable=True)
    rejection_reason = Column(String, nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    client_order_id = Column(String, nullable=False, unique=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    quantity = Column(Float, nullable=False)
    order_type = Column(String, nullable=False)
    price = Column(Float, nullable=True)
    reduce_only = Column(Boolean, nullable=False, default=False)
    status = Column(String, nullable=False)
    filled_quantity = Column(Float, nullable=False, default=0.0)
    avg_fill_price = Column(Float, nullable=True)
    fee = Column(Float, nullable=False, default=0.0)
    created_at = Column(Integer, nullable=False)
    signal_id = Column(String, nullable=True, index=True)
    tag = Column(String, nullable=True)


class PositionRow(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(String, nullable=False, unique=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    initial_quantity = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profits = Column(JSON, nullable=True)
    opened_at = Column(Integer, nullable=False)
    wave_label = Column(String, nullable=False)
    signal_id = Column(String, nullable=False, index=True)
    risk_amount = Column(Float, nullable=False)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    closed = Column(Boolean, nullable=False, default=False)
    closed_at = Column(Integer, nullable=True)
    mae = Column(Float, nullable=False, default=0.0)
    mfe = Column(Float, nullable=False, default=0.0)


class BacktestResultRow(Base):
    __tablename__ = "backtest_results"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False)
    wave_label = Column(String, nullable=False)
    trades = Column(Integer, nullable=False)
    winrate = Column(Float, nullable=False)
    profit_factor = Column(Float, nullable=True)
    expectancy = Column(Float, nullable=False)
    sharpe = Column(Float, nullable=True)
    sortino = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=False)
    average_r = Column(Float, nullable=False)
    median_r = Column(Float, nullable=False)
    mae_avg = Column(Float, nullable=True)
    mfe_avg = Column(Float, nullable=True)
    created_at = Column(Integer, nullable=False)
