"""Strategy registry.

Every strategy module exposes a top-level ``analyze(symbol, bars_1m, bars_5m,
bars_1d, account, positions) -> dict`` function with the shape documented in
each module. ``STRATEGIES`` is the ordered list the signal engine iterates
over. ``market_regime`` is meta — it classifies the regime and recommends
weights, so it is NOT in ``STRATEGIES`` (the engine calls it separately).
"""

from __future__ import annotations

from . import (
    ema_crossover,
    momentum_breakout,
    rsi_divergence,
    squeeze_momentum,
    support_resistance,
    volume_profile,
    vwap_reversion,
)
from . import market_regime  # meta — referenced directly by signal_engine

# Order is informational only; the aggregator uses weighted voting.
STRATEGIES = [
    vwap_reversion,
    momentum_breakout,
    ema_crossover,
    rsi_divergence,
    volume_profile,
    squeeze_momentum,
    support_resistance,
]

__all__ = [
    "STRATEGIES",
    "ema_crossover",
    "market_regime",
    "momentum_breakout",
    "rsi_divergence",
    "squeeze_momentum",
    "support_resistance",
    "volume_profile",
    "vwap_reversion",
]
