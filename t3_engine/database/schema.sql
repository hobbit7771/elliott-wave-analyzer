-- T3 Engine reference schema (spec section 26).
--
-- This mirrors what SQLAlchemy (models.py) creates automatically for
-- SQLite/Postgres. Run this file directly ONLY if you are deploying to
-- Postgres/TimescaleDB and want hypertables for the two high-volume
-- time-series tables - `Base.metadata.create_all()` cannot do that part
-- (hypertable conversion is a Timescale-specific extension function, not
-- portable SQL/DDL), which is why it is called out here explicitly rather
-- than silently skipped.

CREATE TABLE IF NOT EXISTS raw_trades (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    is_buyer_maker BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_trades_symbol_ts ON raw_trades (symbol, timestamp);

CREATE TABLE IF NOT EXISTS candles (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time BIGINT NOT NULL,
    close_time BIGINT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    taker_buy_volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    trades INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_time ON candles (symbol, timeframe, open_time);

-- Run these two ONLY on TimescaleDB (Postgres extension), after the
-- tables above exist:
--   SELECT create_hypertable('raw_trades', 'timestamp', chunk_time_interval => 3600000, if_not_exists => true);
--   SELECT create_hypertable('candles', 'open_time', chunk_time_interval => 86400000, if_not_exists => true);

CREATE TABLE IF NOT EXISTS wave_states (
    id BIGSERIAL PRIMARY KEY,
    wave_id TEXT NOT NULL UNIQUE,
    parent_wave_id TEXT,
    symbol TEXT NOT NULL,
    degree TEXT NOT NULL,
    label TEXT NOT NULL,
    direction TEXT NOT NULL,
    start_timestamp BIGINT NOT NULL,
    end_timestamp BIGINT NOT NULL,
    start_price DOUBLE PRECISION NOT NULL,
    end_price DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    invalid_level DOUBLE PRECISION,
    fib_relationships JSONB,
    subwave_count INTEGER NOT NULL DEFAULT 0,
    structure_type TEXT
);

CREATE TABLE IF NOT EXISTS wave_scenarios (
    id BIGSERIAL PRIMARY KEY,
    scenario_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    degree TEXT NOT NULL,
    probability DOUBLE PRECISION NOT NULL,
    elliott_validity DOUBLE PRECISION NOT NULL,
    fib_score DOUBLE PRECISION NOT NULL,
    price_action_score DOUBLE PRECISION NOT NULL,
    volume_score DOUBLE PRECISION NOT NULL,
    momentum_score DOUBLE PRECISION NOT NULL,
    microstructure_score DOUBLE PRECISION NOT NULL,
    derivatives_score DOUBLE PRECISION NOT NULL,
    invalidation DOUBLE PRECISION,
    expected_target DOUBLE PRECISION,
    status TEXT NOT NULL,
    wave_ids JSONB,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    data_available_at_signal BIGINT NOT NULL,
    side TEXT NOT NULL,
    wave_label TEXT NOT NULL,
    entry_stage TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    score_breakdown JSONB,
    entry_zone_low DOUBLE PRECISION,
    entry_zone_high DOUBLE PRECISION,
    stop_loss DOUBLE PRECISION NOT NULL,
    take_profits JSONB,
    risk_reward DOUBLE PRECISION NOT NULL,
    invalidation DOUBLE PRECISION NOT NULL,
    wave_state_at_signal JSONB,
    reason TEXT,
    decision TEXT NOT NULL,
    scenario_id TEXT,
    rejection_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals (symbol, created_at);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    order_type TEXT NOT NULL,
    price DOUBLE PRECISION,
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL,
    filled_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_fill_price DOUBLE PRECISION,
    fee DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    signal_id TEXT,
    tag TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    position_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    initial_quantity DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION NOT NULL,
    take_profits JSONB,
    opened_at BIGINT NOT NULL,
    wave_label TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    risk_amount DOUBLE PRECISION NOT NULL,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    closed BOOLEAN NOT NULL DEFAULT FALSE,
    closed_at BIGINT,
    mae DOUBLE PRECISION NOT NULL DEFAULT 0,
    mfe DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    wave_label TEXT NOT NULL,
    trades INTEGER NOT NULL,
    winrate DOUBLE PRECISION NOT NULL,
    profit_factor DOUBLE PRECISION,
    expectancy DOUBLE PRECISION NOT NULL,
    sharpe DOUBLE PRECISION,
    sortino DOUBLE PRECISION,
    max_drawdown DOUBLE PRECISION NOT NULL,
    average_r DOUBLE PRECISION NOT NULL,
    median_r DOUBLE PRECISION NOT NULL,
    mae_avg DOUBLE PRECISION,
    mfe_avg DOUBLE PRECISION,
    created_at BIGINT NOT NULL
);
