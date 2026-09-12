"""Risk management (spec section 14).

Every rule here is a hard gate, not a suggestion:
  - position size is DERIVED from (risk_amount / stop_distance), never the
    other way around;
  - a stop can never be widened to "save" a trade;
  - breaching the daily or total drawdown limit sets TRADING_DISABLED and
    the manager will refuse every subsequent `can_open_trade` call until
    explicitly reset (a human/ops decision, not something the bot itself
    should silently undo).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from t3_engine.common.types import Direction, WaveLabel

DEFAULT_RISK_PER_WAVE: Dict[WaveLabel, float] = {
    WaveLabel.W3: 0.0100,
    WaveLabel.W4: 0.0050,
    WaveLabel.W5: 0.0075,
    WaveLabel.C: 0.0100,
}


class TradingDisabledError(Exception):
    pass


@dataclass
class OpenRiskEntry:
    position_id: str
    wave_label: WaveLabel
    risk_amount: float


@dataclass
class RiskManager:
    initial_equity: float
    max_daily_drawdown: float = 0.03
    max_total_drawdown: float = 0.12
    max_concurrent_trades: int = 3
    max_correlated_exposure: float = 0.02  # fraction of equity, summed risk of open trades
    risk_per_wave: Dict[WaveLabel, float] = field(default_factory=lambda: dict(DEFAULT_RISK_PER_WAVE))

    equity: float = field(init=False)
    peak_equity: float = field(init=False)
    day_start_equity: float = field(init=False)
    trading_enabled: bool = field(init=False, default=True)
    disabled_reason: Optional[str] = field(init=False, default=None)
    open_risk: Dict[str, OpenRiskEntry] = field(default_factory=dict)

    def __post_init__(self):
        self.equity = self.initial_equity
        self.peak_equity = self.initial_equity
        self.day_start_equity = self.initial_equity

    def risk_amount_for(self, wave_label: WaveLabel) -> float:
        pct = self.risk_per_wave.get(wave_label)
        if pct is None:
            raise ValueError(f"{wave_label} is not a tradeable wave; no risk % configured")
        return self.equity * pct

    def position_size(self, wave_label: WaveLabel, entry_price: float, stop_price: float) -> float:
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            raise ValueError("stop_distance must be > 0 to size a position")
        risk_amount = self.risk_amount_for(wave_label)
        return risk_amount / stop_distance

    def can_open_trade(self, wave_label: WaveLabel) -> tuple:
        if not self.trading_enabled:
            return False, f"TRADING_DISABLED: {self.disabled_reason}"
        if len(self.open_risk) >= self.max_concurrent_trades:
            return False, f"MAX_CONCURRENT_TRADES_REACHED: {len(self.open_risk)}/{self.max_concurrent_trades}"
        proposed_risk = self.risk_amount_for(wave_label)
        current_total_risk = sum(e.risk_amount for e in self.open_risk.values())
        if (current_total_risk + proposed_risk) / self.equity > self.max_correlated_exposure:
            return False, (f"MAX_CORRELATED_EXPOSURE_EXCEEDED: "
                            f"{(current_total_risk + proposed_risk) / self.equity:.4f} > {self.max_correlated_exposure}")
        return True, "OK"

    def register_trade_opened(self, position_id: str, wave_label: WaveLabel, risk_amount: float) -> None:
        self.open_risk[position_id] = OpenRiskEntry(position_id, wave_label, risk_amount)

    def register_trade_closed(self, position_id: str, realized_pnl: float) -> None:
        self.open_risk.pop(position_id, None)
        self.equity += realized_pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self._check_drawdown_limits()

    def start_new_day(self) -> None:
        self.day_start_equity = self.equity

    def _check_drawdown_limits(self) -> None:
        if self.day_start_equity > 0:
            daily_dd = (self.day_start_equity - self.equity) / self.day_start_equity
            if daily_dd >= self.max_daily_drawdown:
                self._disable(f"daily drawdown {daily_dd:.2%} >= limit {self.max_daily_drawdown:.2%}")
                return
        if self.peak_equity > 0:
            total_dd = (self.peak_equity - self.equity) / self.peak_equity
            if total_dd >= self.max_total_drawdown:
                self._disable(f"total drawdown {total_dd:.2%} >= limit {self.max_total_drawdown:.2%}")

    def _disable(self, reason: str) -> None:
        self.trading_enabled = False
        self.disabled_reason = reason

    def manual_reset(self, new_equity: Optional[float] = None) -> None:
        """Explicit human/ops re-enable after a drawdown halt. Never called
        automatically by the engine itself."""
        self.trading_enabled = True
        self.disabled_reason = None
        if new_equity is not None:
            self.equity = new_equity
            self.peak_equity = max(self.peak_equity, new_equity)


def validate_stop_not_widened(original_stop: float, proposed_stop: float, side_direction: Direction) -> bool:
    """Section 14: 'НИКОГДА не расширять стоп ради спасения сделки.' A
    stop may only ever move to REDUCE risk (tighten), never widen it."""
    if side_direction == Direction.UP:  # long: stop is below entry, tightening = moving UP
        return proposed_stop >= original_stop
    return proposed_stop <= original_stop  # short: stop is above entry, tightening = moving DOWN
