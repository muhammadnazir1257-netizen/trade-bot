"""Market regime classifier (meta-strategy).

Doesn't generate a trade signal — instead it inspects SPY's daily bars and
returns a regime label plus a recommended set of strategy weight
multipliers. The signal aggregator applies those multipliers on top of
``config.STRATEGY_WEIGHTS``.

VIX data is not available on the free Alpaca tier, so the classifier uses
ATR / SMA(ATR) as a volatility proxy. The thresholds are calibrated against
historical SPY behavior.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_THIS_DIR)
_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _d in (_SCRIPTS_DIR, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import indicators as ind  # noqa: E402
import config             # noqa: E402

NAME = "market_regime"
HIGH_VOL_ATR_MULT = 2.0   # ATR > 2× SMA-20 of ATR ⇒ HIGH_VOLATILITY
EMA_TREND_PERIOD = 50


def classify(spy_bars_1d: list[dict], breadth_pct: float | None = None) -> dict[str, Any]:
    """Classify the current market regime from SPY daily bars.

    ``breadth_pct`` (optional): fraction of watchlist symbols above their
    20-day SMA, in [0, 1]. Currently an ANNOTATION input only — label
    changes based on breadth require replay evidence first (see CLAUDE.md
    §8 Validation Discipline).

    Returns ``{"regime": str, "weights": dict, "details": str}``. ``weights``
    is the regime-specific override mapping from config.
    """
    if not spy_bars_1d or len(spy_bars_1d) < EMA_TREND_PERIOD + config.ATR_PERIOD + 5:
        return _regime("UNKNOWN", "insufficient_spy_bars")

    _, highs, lows, closes, _ = ind.extract_ohlcv(spy_bars_1d)
    ema50 = ind.ema(closes, EMA_TREND_PERIOD)
    adx_arr = ind.adx(highs, lows, closes, period=config.ADX_PERIOD)
    atr_arr = ind.atr(highs, lows, closes, period=config.ATR_PERIOD)

    if np.isnan(ema50[-1]) or np.isnan(adx_arr[-1]) or np.isnan(atr_arr[-1]):
        return _regime("UNKNOWN", "indicators_not_warm")

    last_close = float(closes[-1])
    last_ema50 = float(ema50[-1])
    last_adx = float(adx_arr[-1])
    last_atr = float(atr_arr[-1])
    # Average ATR over the trailing 20 bars (exclude the current one)
    valid_atr = atr_arr[~np.isnan(atr_arr)]
    sma_atr = float(np.mean(valid_atr[-20:])) if len(valid_atr) >= 20 else float(np.mean(valid_atr))

    high_vol = sma_atr > 0 and last_atr > HIGH_VOL_ATR_MULT * sma_atr

    # Volatility percentile: where today's ATR sits in the trailing window
    # (VIX itself isn't on the free tier; ATR percentile is the honest proxy).
    vol_pctile = float((valid_atr < last_atr).mean() * 100) if len(valid_atr) > 20 else float("nan")

    details = (f"SPY ${last_close:.2f} vs EMA{EMA_TREND_PERIOD} ${last_ema50:.2f}; "
               f"ADX {last_adx:.1f}; ATR {last_atr:.2f} vs 20-bar avg {sma_atr:.2f}")
    if not np.isnan(vol_pctile):
        details += f"; vol pctile {vol_pctile:.0f}"
    if breadth_pct is not None:
        details += f"; breadth {breadth_pct:.0%} above 20d SMA"

    if high_vol:
        return _regime("HIGH_VOLATILITY", details)
    if last_adx >= config.ADX_TREND_THRESHOLD:
        if last_close > last_ema50:
            return _regime("TRENDING_UP", details)
        return _regime("TRENDING_DOWN", details)
    if last_adx < config.ADX_RANGE_THRESHOLD:
        return _regime("RANGING", details)
    # Choppy / mild trend — default to RANGING with the existing weights
    return _regime("RANGING", details + " [low-conviction trend → ranging weights]")


def _regime(name: str, details: str) -> dict[str, Any]:
    weights = config.REGIME_WEIGHT_MULTIPLIERS.get(name, {})
    return {"regime": name, "weights": weights, "details": details}


# Optional thin wrapper so the meta-strategy can also be invoked via the
# common analyze() signature (returns a HOLD signal — never trades).
def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    out = classify(bars_1d) if symbol.upper() == "SPY" else {
        "regime": "UNKNOWN", "weights": {}, "details": "regime classifier only runs on SPY",
    }
    return {
        "signal": "HOLD",
        "confidence": 0.0,
        "reason": f"regime classifier — {out['regime']}: {out.get('details', '')}",
        "strategy": NAME,
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
    }
