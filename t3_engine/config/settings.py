"""Central configuration, loaded from environment variables / .env.

Every risk and behavioural knob mentioned in the spec (section 14) is a
field here, never a hardcoded literal buried in engine code.
"""

from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

from t3_engine.common.types import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="T3_", extra="ignore")

    # --- exchange / data ---
    binance_futures_rest_base: str = "https://fapi.binance.com"
    binance_futures_ws_base: str = "wss://fstream.binance.com"
    binance_api_key: str = ""
    binance_api_secret: str = ""
    symbols: List[str] = ["BTCUSDT"]
    historical_klines_limit: int = 1500

    # --- mode ---
    trading_mode: TradingMode = TradingMode.PAPER

    # --- risk management (section 14), values are % of equity as fraction ---
    risk_wave3: float = 0.0100
    risk_wave4: float = 0.0050
    risk_wave5: float = 0.0075
    risk_wave_c: float = 0.0100
    max_daily_drawdown: float = 0.03
    max_total_drawdown: float = 0.12
    max_concurrent_trades: int = 3
    max_correlated_exposure: float = 0.02  # sum of risk % across correlated open trades

    # --- signal engine (section 23) ---
    entry_confidence_threshold: float = 75.0
    weight_elliott: float = 0.30
    weight_price_action: float = 0.20
    weight_fibonacci: float = 0.15
    weight_volume: float = 0.10
    weight_momentum: float = 0.10
    weight_derivatives: float = 0.05
    weight_orderbook: float = 0.05
    weight_higher_tf: float = 0.05

    # --- execution ---
    taker_fee_bps: float = 4.0
    simulated_slippage_bps: float = 2.0

    # --- misc ---
    database_url: str = "sqlite:///./t3_engine.db"
    log_dir: str = "./logs"
    initial_equity: float = 10_000.0


settings = Settings()
