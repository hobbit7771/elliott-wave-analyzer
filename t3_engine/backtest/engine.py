"""Event-driven, causal backtester (spec section 19 + 16/20).

Wires together every module built so far into the same pipeline order the
live engine uses (section 20):

    candle -> pivot detector -> market structure -> scenario engine
    -> signal engine (score + evaluate_entry) -> risk engine
    -> execution (paper) -> position manager -> metrics

KNOWN SIMPLIFICATION (flagged honestly, section 30): the live spec wants
entry timing (PREDICTION/SETUP/ARMED/TRIGGERED) driven by genuine 1s-3m
microstructure confirmation (section 21). This backtester only replays a
single timeframe (5m/15m candles) end-to-end, so it cannot reconstruct
that sub-minute confirmation sequence from history - Binance does not
serve historical sub-minute klines at all (see market_data/rest_client.py),
and full aggTrade-based reconstruction is only possible for a recent
retention window, not for deep multi-year history. As the nearest correct
stand-in, a wave transition confirmed on the primary timeframe is treated
as reaching TRIGGERED immediately. This is documented, not hidden, and is
exactly the kind of thing a real deployment should replace by re-running
the backtester over a shorter window using reconstructed aggTrade-based
sub-minute bars (real_time pipeline in `pipeline/live_loop.py` already
does use genuine sub-minute confirmation, since it runs on live data).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from t3_engine.common.models import Candle
from t3_engine.common.types import (
    Direction,
    EntryStage,
    Timeframe,
    TradeSide,
    TRADEABLE_TIMEFRAMES,
    TRADEABLE_WAVES,
    WaveLabel,
)
from t3_engine.elliott_engine.scenario import ScenarioEngine
from t3_engine.execution.paper import PaperExecutionEngine
from t3_engine.market_structure.pivots import ZigZagPivotDetector
from t3_engine.market_structure.structure import MarketStructureTracker
from t3_engine.orderflow.flow import taker_flow_supports_direction
from t3_engine.orderflow.momentum import ADX, EMA, MACD, momentum_score
from t3_engine.position_manager.manager import PositionManager
from t3_engine.risk_engine.risk_manager import RiskManager
from t3_engine.signal_engine.scoring import ScoreBreakdown, evaluate_entry
from t3_engine.signal_engine.targets import plan_wave3_long, plan_wave4_short, plan_wave5_long, plan_wave_c_short


@dataclass
class BacktestConfig:
    symbol: str
    initial_equity: float = 10_000.0
    pivot_deviation_pct: float = 1.0
    structure_min_break_pct: float = 0.05
    entry_confidence_threshold: float = 75.0
    degree: Timeframe = Timeframe.M5


class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.pivot_detector = ZigZagPivotDetector(deviation_pct=config.pivot_deviation_pct)
        self.structure = MarketStructureTracker(min_break_pct=config.structure_min_break_pct)
        self.scenario_engine = ScenarioEngine(degree=config.degree)
        self.risk_manager = RiskManager(initial_equity=config.initial_equity)
        self.execution = PaperExecutionEngine()
        self.position_manager = PositionManager(self.execution, self.risk_manager)
        self.signals: List = []
        self.macd = MACD()
        self.adx = ADX()
        self.ema9 = EMA(9)
        self.ema18 = EMA(18)

    def run(self, candles: List[Candle]) -> Dict:
        for i, candle in enumerate(candles):
            self.process_candle(candle, i, candles)

        return {
            "closed_positions": self.position_manager.closed_positions,
            "open_positions": [p for p in self.position_manager.positions.values() if not p.closed],
            "signals": self.signals,
            "final_equity": self.risk_manager.equity,
        }

    def process_candle(self, candle: Candle, index: int, history: List[Candle]) -> None:
        """Process exactly one CLOSED candle. `history` must be the full
        list of candles seen so far (index `index` being the current one) -
        used only for bounded lookback (recent taker-flow window etc.),
        never peeked into beyond `index`. Split out from `run()` so the
        live pipeline (pipeline/live_loop.py) can feed real-time closed
        candles one at a time through the exact same tested logic instead
        of a duplicated implementation."""
        if not candle.closed:
            raise ValueError("Backtester must only be fed CLOSED candles (no-lookahead guarantee)")

        self.ema9.update(candle.close)
        self.ema18.update(candle.close)
        self.macd.update(candle.close)
        self.adx.update(candle.high, candle.low, candle.close)

        self._update_open_positions(candle)

        pivot = self.pivot_detector.update(index, candle)
        if pivot is not None:
            self.structure.on_pivot(pivot)
            direction = self.structure.trend or Direction.UP
            scenarios = self.scenario_engine.rebuild(self.pivot_detector.pivots[:index + 1], direction)
            self._maybe_open_trade(scenarios, direction, candle, index, history)

    def _update_open_positions(self, candle: Candle) -> None:
        for position_id in list(self.position_manager.positions.keys()):
            pos = self.position_manager.positions[position_id]
            if pos.closed:
                continue
            # Conservative fill ordering within the bar: check the adverse
            # extreme first (protects against overstating performance if
            # both the stop and a TP technically sit inside one bar's range).
            if pos.side == TradeSide.LONG:
                self.position_manager.on_price_update(position_id, candle.low, candle.close_time)
                if position_id in self.position_manager.positions and not self.position_manager.positions[position_id].closed:
                    self.position_manager.on_price_update(position_id, candle.high, candle.close_time)
            else:
                self.position_manager.on_price_update(position_id, candle.high, candle.close_time)
                if position_id in self.position_manager.positions and not self.position_manager.positions[position_id].closed:
                    self.position_manager.on_price_update(position_id, candle.low, candle.close_time)

    def _maybe_open_trade(self, scenarios, direction: Direction, candle: Candle, index: int, candles: List[Candle]) -> None:
        if self.config.degree not in TRADEABLE_TIMEFRAMES:
            # Section 21/spec: only 5m/15m may ever originate a real entry -
            # 1s-3m is confirmation-only and 1h/4h is context-only. This
            # engine is otherwise timeframe-agnostic (candles/structure/
            # scenarios all work identically at any degree - see the
            # dashboard's per-timeframe chart view), so without this guard
            # a non-tradeable degree would silently open real paper trades
            # on a timeframe the spec explicitly says never should.
            return
        if not scenarios:
            return
        top = scenarios[0]
        wave = top.current_wave
        if wave is None:
            return

        side = TRADEABLE_WAVES.get(wave.label, TradeSide.NONE)
        if side == TradeSide.NONE:
            return

        if any(not p.closed and p.wave_label == wave.label for p in self.position_manager.positions.values()):
            return  # one position per wave label at a time in this simplified backtest

        by_label = {w.label: w for w in top.waves}
        entry_price = candle.close
        plan = None
        if wave.label == WaveLabel.W3 and WaveLabel.W1 in by_label and WaveLabel.W2 in by_label:
            plan = plan_wave3_long(by_label[WaveLabel.W1], by_label[WaveLabel.W2], entry_price)
        elif wave.label == WaveLabel.W4 and WaveLabel.W3 in by_label:
            plan = plan_wave4_short(by_label[WaveLabel.W3], entry_price)
        elif wave.label == WaveLabel.W5 and WaveLabel.W1 in by_label and WaveLabel.W4 in by_label:
            plan = plan_wave5_long(by_label[WaveLabel.W1], by_label[WaveLabel.W4], entry_price)
        elif wave.label == WaveLabel.C and WaveLabel.A in by_label and WaveLabel.B in by_label:
            plan = plan_wave_c_short(by_label[WaveLabel.A], by_label[WaveLabel.B], entry_price)
        if plan is None:
            return

        recent = candles[max(0, index - 10):index + 1]
        price_action_score = 1.0 if self.structure.events and self.structure.events[-1].breaking_index == index else 0.5
        volume_score = 1.0 if taker_flow_supports_direction(recent, direction) else 0.3
        momentum = momentum_score(self.macd, self.adx, self.ema9.value or entry_price,
                                   self.ema18.value or entry_price, direction_is_up=(side == TradeSide.LONG))

        breakdown = ScoreBreakdown(
            elliott=top.elliott_validity, price_action=price_action_score, fibonacci=top.fib_score,
            volume=volume_score, momentum=momentum, derivatives=0.5, orderbook=0.5, higher_tf=0.5,
        )

        signal = evaluate_entry(
            symbol=self.config.symbol, scenario=top, wave_label=wave.label,
            entry_stage=EntryStage.TRIGGERED,  # see module docstring: single-TF backtest simplification
            breakdown=breakdown, entry_zone=plan.entry_zone, stop_loss=plan.stop_loss,
            take_profits=plan.take_profits, risk_reward=plan.risk_reward, invalidation=plan.invalidation,
            now_ms=candle.close_time, data_available_at=candle.close_time,
            threshold=self.config.entry_confidence_threshold,
        )
        self.signals.append(signal)

        if signal.decision != "SIGNAL_ACCEPTED":
            return

        risk_ok, reason = self.risk_manager.can_open_trade(wave.label)
        if not risk_ok:
            signal.decision = "SIGNAL_REJECTED"
            signal.rejection_reason = reason
            signal.reason = reason
            return

        quantity = self.risk_manager.position_size(wave.label, entry_price, plan.stop_loss)
        self.position_manager.open_position(
            signal=signal, symbol=self.config.symbol, side=side, entry_price=entry_price,
            quantity=quantity, stop_loss=plan.stop_loss, take_profits=plan.take_profits,
            wave_label=wave.label, now_ms=candle.close_time,
        )
