"""Backtest performance metrics (spec section 19)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional

from t3_engine.common.models import Position


@dataclass
class Metrics:
    trades: int
    winrate: float
    profit_factor: Optional[float]
    expectancy: float
    sharpe: Optional[float]
    sortino: Optional[float]
    max_drawdown: float
    average_r: float
    median_r: float
    mae_avg: float
    mfe_avg: float


def _equity_curve_max_drawdown(pnls: List[float], starting_equity: float) -> float:
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def compute_metrics(positions: List[Position], starting_equity: float = 10_000.0) -> Metrics:
    if not positions:
        return Metrics(0, 0.0, None, 0.0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0)

    pnls = [p.realized_pnl for p in positions]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    r_multiples = [p.realized_pnl / p.risk_amount for p in positions if p.risk_amount]
    average_r = statistics.mean(r_multiples) if r_multiples else 0.0
    median_r = statistics.median(r_multiples) if r_multiples else 0.0

    sharpe = sortino = None
    if len(r_multiples) >= 2:
        stdev = statistics.pstdev(r_multiples)
        if stdev > 0:
            sharpe = statistics.mean(r_multiples) / stdev
        downside = [r for r in r_multiples if r < 0]
        if downside:
            downside_std = statistics.pstdev(downside)
            if downside_std > 0:
                sortino = statistics.mean(r_multiples) / downside_std

    return Metrics(
        trades=len(positions),
        winrate=len(wins) / len(pnls),
        profit_factor=profit_factor,
        expectancy=statistics.mean(pnls),
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=_equity_curve_max_drawdown(pnls, starting_equity),
        average_r=average_r,
        median_r=median_r,
        mae_avg=statistics.mean([p.mae for p in positions]),
        mfe_avg=statistics.mean([p.mfe for p in positions]),
    )


def compute_metrics_by_wave(positions: List[Position], starting_equity: float = 10_000.0) -> Dict[str, Metrics]:
    """Section 19: report Wave 3 LONG / Wave 4 SHORT / Wave 5 LONG / Wave C
    SHORT separately, never pooled into one blended number."""
    by_label: Dict[str, List[Position]] = {}
    for p in positions:
        key = f"{p.wave_label.value}_{p.side.value}"
        by_label.setdefault(key, []).append(p)
    return {label: compute_metrics(group, starting_equity) for label, group in by_label.items()}
