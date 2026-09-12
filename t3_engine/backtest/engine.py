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
from t3_engine.elliott_engine.scenario import ScenarioEngine, build_subwaves
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
    # Subwave detection (spec follow-up: "waves and subwaves should be
    # accounted for") re-runs pivot detection at a FINER deviation than the
    # primary count over just one motive wave's own candle range - this is
    # that finer threshold. Half the primary one by default: a subwave is,
    # by definition, a smaller move than the wave it subdivides.
    subwave_deviation_pct: float = 0.5

# Only motive waves (1, 3, 5) subdivide into a 5-wave count in Elliott
# theory - corrective waves (2, 4, A, B, C) subdivide into 3, which this
# engine doesn't build subwave labels for yet (see WaveLabel's docstring:
# only i-v micro-labels exist, no micro a-b-c).
_MOTIVE_LABELS_FOR_SUBWAVES = (WaveLabel.W1, WaveLabel.W3, WaveLabel.W5)
_MIN_CANDLES_FOR_SUBWAVES = 6  # need at least enough bars for 5 legs' worth of pivots


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
        # Subwaves, grouped under the confirmed-chain wave they belong to.
        # Keyed by (parent_label, parent_start_timestamp) rather than by
        # parent wave_id because rebuild() recreates Wave objects (and
        # therefore ids) on every pivot - label+start is what stays stable
        # for the same logical wave. Grouping this way is also what makes
        # pruning possible: when the chain truncates, the subwaves of the
        # waves it dropped go with them instead of outliving their parent.
        self.subwaves_by_parent: Dict[tuple, List] = {}
        self._subwaves_attempted: set = set()

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
            self._update_subwaves(history)
            self._maybe_open_trade(scenarios, direction, candle, index, history)

    @property
    def subwave_history(self) -> List:
        """Every subwave currently attached to a wave still in the
        confirmed chain, oldest first."""
        flat = [sub for subs in self.subwaves_by_parent.values() for sub in subs]
        return sorted(flat, key=lambda w: w.start_timestamp)

    def _update_subwaves(self, history: List[Candle]) -> None:
        """Subdivide each motive (1/3/5) wave of the confirmed chain into
        its own i-ii-iii-iv-v count, and drop the subwaves of any wave the
        chain no longer contains.

        A wave only enters the chain once the top-level count has moved
        past it, so its candle range is already fixed and the subdivision
        is a one-shot computation - attempted exactly once per wave. The
        pruning half matters just as much: when a re-anchor truncates the
        chain, the subwaves underneath the dropped waves have to go too,
        or they'd outlive the count they were derived from and become
        exactly the kind of orphaned label the chain exists to prevent."""
        chain_keys = {(w.label, w.start_timestamp) for w in self.scenario_engine.confirmed_chain}

        for stale_key in set(self.subwaves_by_parent) - chain_keys:
            del self.subwaves_by_parent[stale_key]
        self._subwaves_attempted &= chain_keys

        for wave in self.scenario_engine.confirmed_chain:
            key = (wave.label, wave.start_timestamp)
            if key in self._subwaves_attempted:
                continue
            self._subwaves_attempted.add(key)
            if wave.label not in _MOTIVE_LABELS_FOR_SUBWAVES:
                continue
            wave_candles = [c for c in history if wave.start_timestamp <= c.open_time <= wave.end_timestamp]
            if len(wave_candles) < _MIN_CANDLES_FOR_SUBWAVES:
                continue
            built = build_subwaves(wave_candles, wave, self.config.subwave_deviation_pct)
            if built["waves"]:
                self.subwaves_by_parent[key] = built["waves"]

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
            # TRADEABLE_TIMEFRAMES (common/types.py) is (M5, M15, H1, H4) -
            # widened from the spec-section-21 original (M5, M15) on the
            # project owner's explicit instruction to trade 1h/4h too; only
            # 1s-3m stays confirmation-only. This engine is otherwise
            # timeframe-agnostic (candles/structure/scenarios all work
            # identically at any degree - see the dashboard's per-timeframe
            # chart view), so without this guard a confirmation-only degree
            # would silently open real paper trades too.
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
