"""Every knob the Lead Engine has, in one place, read from the environment.

Separate from t3_engine/config/settings.py on purpose. That file is the
older system's configuration and changing it would mean the two systems
share a failure mode: a typo in a Lead Engine weight would break the
trading settings object that the backtester and the risk engine load at
import time. These settings stand alone and are read lazily, so a bad
value here can only ever break this engine.

Environment prefix is T3_LEAD_ENGINE_ so nothing collides with the older
T3_ settings. The one name the specification fixes exactly -
LEAD_ENGINE_ENABLED - is accepted in both spellings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# The flag. Accepted as LEAD_ENGINE_ENABLED (as specified) or with the
# project's T3_ prefix, because every other variable this deployment sets
# carries that prefix and a lone unprefixed name is easy to lose.
ENABLED_ENV = "LEAD_ENGINE_ENABLED"
ENABLED_ENV_PREFIXED = "T3_LEAD_ENGINE_ENABLED"

_TRUE = {"1", "true", "yes", "on"}

# The reference instrument. It is not just another row in the symbol list:
# the lead-lag module needs it subscribed and flowing whether or not
# anyone is looking at it, because "did BTC move first" is unanswerable
# without a continuous BTC stream.
BTC_SYMBOL = "BTCUSDT"

DEFAULT_SYMBOLS: Tuple[str, ...] = (
    "BTCUSDT", "INJUSDT", "DOGEUSDT", "AAVEUSDT",
    "ATOMUSDT", "FILUSDT", "NEARUSDT",
)

# Kline intervals, in Bybit's own notation (minutes as bare numbers).
DEFAULT_KLINE_INTERVALS: Tuple[str, ...] = ("1", "5", "15", "60", "240")

# Order book depth topic. 50 is what the specification asks for and what
# the book keeps; asking for a deeper topic would cost bandwidth for
# levels no feature here reads.
ORDERBOOK_DEPTH = 50

PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
REST_BASE = "https://api.bybit.com"


def enabled() -> bool:
    """Read at call time, never cached at import.

    A module-level constant would freeze whatever the environment looked
    like when the first import happened, which in a test run is whatever
    the previous test left behind."""
    for name in (ENABLED_ENV, ENABLED_ENV_PREFIXED):
        raw = os.getenv(name)
        if raw is not None:
            return raw.strip().lower() in _TRUE
    return False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(f"T3_LEAD_ENGINE_{name}", "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(f"T3_LEAD_ENGINE_{name}", "").strip() or default)
    except ValueError:
        return default


def symbols() -> List[str]:
    raw = os.getenv("T3_LEAD_ENGINE_SYMBOLS", "").strip()
    listed = [s.strip().upper() for s in raw.split(",") if s.strip()] if raw else list(DEFAULT_SYMBOLS)
    # BTC is machinery, not a preference - see BTC_SYMBOL.
    if BTC_SYMBOL not in listed:
        listed.insert(0, BTC_SYMBOL)
    return listed


@dataclass(frozen=True)
class LayerWeights:
    """How much each of the five independent layers counts.

    The brief's opening numbers. They are a starting point and are said to
    be one: the right weights are whatever a calibrated backtest says they
    are, and until there is enough history for that these are a guess with
    a rationale - flow first because aggression is what actually moves
    price in the next minute, book second because it is what aggression
    meets, structure third because it says where the move matters, and
    positioning and BTC as context rather than cause."""

    flow: float = 0.30
    book: float = 0.25
    structure: float = 0.20
    derivatives: float = 0.15
    btc_lead: float = 0.10

    def as_dict(self) -> Dict[str, float]:
        return {"flow": self.flow, "book": self.book, "structure": self.structure,
                "derivatives": self.derivatives, "btc_lead": self.btc_lead}

    def total(self) -> float:
        return sum(self.as_dict().values())


def layer_weights_from_env() -> LayerWeights:
    """Each layer overridable on its own, e.g.
    T3_LEAD_ENGINE_LAYER_FLOW=0.35."""
    defaults = LayerWeights()
    values = {}
    for name, default in defaults.as_dict().items():
        values[name] = _env_float(f"LAYER_{name.upper()}", default)
    return LayerWeights(**values)


@dataclass(frozen=True)
class PressureWeights:
    """What each component contributes to the pressure score.

    The specification's opening numbers, kept as data rather than spread
    through pressure_engine.py as literals - the whole point of a weighted
    score is that the weights are a thing you can look at, argue with and
    change without touching the arithmetic."""

    order_book_imbalance: float = 0.18
    microprice: float = 0.10
    cvd: float = 0.18
    trade_velocity: float = 0.10
    liquidity_shift: float = 0.10
    liquidations: float = 0.12
    btc_lead_lag: float = 0.08
    smc: float = 0.08
    elliott_context: float = 0.06

    def as_dict(self) -> Dict[str, float]:
        return {
            "order_book_imbalance": self.order_book_imbalance,
            "microprice": self.microprice,
            "cvd": self.cvd,
            "trade_velocity": self.trade_velocity,
            "liquidity_shift": self.liquidity_shift,
            "liquidations": self.liquidations,
            "btc_lead_lag": self.btc_lead_lag,
            "smc": self.smc,
            "elliott_context": self.elliott_context,
        }

    def total(self) -> float:
        return sum(self.as_dict().values())


def weights_from_env() -> PressureWeights:
    """Weights overridable one at a time, e.g.
    T3_LEAD_ENGINE_WEIGHT_CVD=0.25. Anything not set keeps the default."""
    defaults = PressureWeights()
    values = {}
    for name, default in defaults.as_dict().items():
        values[name] = _env_float(f"WEIGHT_{name.upper()}", default)
    return PressureWeights(**values)


@dataclass(frozen=True)
class Thresholds:
    """Where a number stops being noise. Every one of these is a judgement
    call, so every one of them is configurable - see _env_float."""

    # Trade velocity z-scores the specification names explicitly.
    velocity_zscore_elevated: float = 2.0
    velocity_zscore_extreme: float = 3.0

    # A trade counted as "large" is one this many times the rolling median
    # trade size for the instrument. Relative, never a fixed notional: a
    # $50k print is enormous in FILUSDT and unremarkable in BTCUSDT.
    large_trade_multiple: float = 8.0

    # A resting order counted as a wall, in multiples of the median level
    # size in the visible book.
    wall_multiple: float = 5.0

    # An order book is "imbalanced" past this, on the -1..+1 OBI scale.
    obi_strong: float = 0.35

    # Liquidation velocity (quote units per second) that makes a flush a
    # flush rather than a tick.
    liquidation_velocity: float = 25_000.0
    liquidation_cascade_acceleration: float = 15_000.0

    # How close to a level price must sit to count as "testing" it.
    compression_pct: float = 0.004

    # Probabilities at which the signal machine changes state.
    prebreak_probability: float = 60.0
    high_probability: float = 72.0
    a_plus_probability: float = 85.0

    # ---- data freshness --------------------------------------------
    #
    # Milliseconds, and deliberately tight. An order book on a linear
    # perpetual updates every 20-100ms by construction, so a book a
    # second old is not "a bit behind", it is describing a market that
    # has moved. The two levels are different decisions: DEGRADED says
    # "shown, but do not trust it", SIGNALS_DISABLED says "no opinion at
    # all". Both configurable, because the right numbers depend on the
    # instrument and the host.
    book_age_degraded_ms: float = 1_000.0
    book_age_signals_off_ms: float = 2_500.0
    trade_age_signals_off_ms: float = 1_500.0

    # The older, coarser limits. Kept because the quiet symbols genuinely
    # go a long time without a print and these gate the NOTES rather than
    # the signals.
    max_book_age_seconds: float = 5.0
    max_trade_age_seconds: float = 30.0
    max_ticker_age_seconds: float = 30.0
    max_oi_age_seconds: float = 900.0


def thresholds_from_env() -> Thresholds:
    defaults = Thresholds()
    values = {}
    for name, default in vars(defaults).items():
        values[name] = _env_float(name.upper(), default)
    return Thresholds(**values)


@dataclass
class LeadEngineConfig:
    """The whole configuration, resolved once when the engine starts."""

    enabled: bool = False
    symbols: List[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    ws_url: str = PUBLIC_LINEAR_WS
    rest_base: str = REST_BASE
    kline_intervals: List[str] = field(default_factory=lambda: list(DEFAULT_KLINE_INTERVALS))
    orderbook_depth: int = ORDERBOOK_DEPTH
    weights: PressureWeights = field(default_factory=PressureWeights)
    layer_weights: LayerWeights = field(default_factory=LayerWeights)
    thresholds: Thresholds = field(default_factory=Thresholds)

    # Open interest comes from REST, on its own clock, never from the
    # socket loop - see oi_engine.py.
    oi_poll_seconds: float = 60.0

    # How much per-symbol history the in-process state keeps. Bounded
    # because this runs in a web process that must not grow without limit.
    max_trades_kept: int = 4000
    max_liquidations_kept: int = 500
    max_feature_snapshots: int = 600
    max_signal_history: int = 200

    # How often the feature snapshot is recomputed and published. The raw
    # streams are far faster than this; recomputing every metric on every
    # book delta would burn CPU for numbers nobody reads between frames.
    recompute_interval_seconds: float = 0.25

    @classmethod
    def from_env(cls) -> "LeadEngineConfig":
        return cls(
            enabled=enabled(),
            symbols=symbols(),
            ws_url=os.getenv("T3_LEAD_ENGINE_WS_URL", "").strip() or PUBLIC_LINEAR_WS,
            rest_base=os.getenv("T3_LEAD_ENGINE_REST_BASE", "").strip() or REST_BASE,
            kline_intervals=list(DEFAULT_KLINE_INTERVALS),
            orderbook_depth=_env_int("ORDERBOOK_DEPTH", ORDERBOOK_DEPTH),
            weights=weights_from_env(),
            layer_weights=layer_weights_from_env(),
            thresholds=thresholds_from_env(),
            oi_poll_seconds=_env_float("OI_POLL_SECONDS", 60.0),
            recompute_interval_seconds=_env_float("RECOMPUTE_INTERVAL_SECONDS", 0.25),
        )
