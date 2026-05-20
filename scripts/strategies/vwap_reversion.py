"""VWAP Mean Reversion strategy.

BUY when price dips below the lower VWAP band with RSI(14) < 35.
SELL when price rises above the upper VWAP band with RSI(14) > 65.
Bands = VWAP ± 1.5 × std-dev of typical price.

Inputs: 1-minute bars (intraday VWAP needs the finest granularity available).
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

NAME = "vwap_reversion"
BAND_STD_MULT = 1.5
RSI_OVERSOLD = config.RSI_OVERSOLD
RSI_OVERBOUGHT = config.RSI_OVERBOUGHT
STOP_BEYOND_PCT = 0.005   # 0.5% beyond the band breach


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Run VWAP-reversion analysis on today's 1-minute bars."""
    result = _hold("insufficient_data", symbol)
    if not bars_1m or len(bars_1m) < 30:
        return result

    opens, highs, lows, closes, vols = ind.extract_ohlcv(bars_1m)
    typical = (highs + lows + closes) / 3.0
    vwap_arr = ind.vwap(highs, lows, closes, vols)
    if np.all(np.isnan(vwap_arr)):
        return result

    last_close = float(closes[-1])
    last_vwap = float(vwap_arr[-1])
    # Std deviation of typical price across the session
    sd = float(np.std(typical, ddof=0))
    if sd == 0:
        return _hold("zero_volatility", symbol)
    band_width = BAND_STD_MULT * sd
    upper_band = last_vwap + band_width
    lower_band = last_vwap - band_width

    rsi_arr = ind.rsi(closes, period=config.RSI_PERIOD)
    if np.isnan(rsi_arr[-1]):
        return _hold("rsi_unavailable", symbol)
    last_rsi = float(rsi_arr[-1])

    # Distance outside the band as fraction of band width → confidence
    if last_close < lower_band and last_rsi < RSI_OVERSOLD:
        excess = (lower_band - last_close) / band_width
        confidence = float(min(0.5 + excess, 1.0))
        stop = lower_band * (1 - STOP_BEYOND_PCT)
        return {
            "signal": "BUY",
            "confidence": confidence,
            "reason": (f"price ${last_close:.2f} below lower VWAP band "
                       f"${lower_band:.2f}; RSI {last_rsi:.1f} < {RSI_OVERSOLD}"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(stop),
            "take_profit": last_vwap,
        }
    if last_close > upper_band and last_rsi > RSI_OVERBOUGHT:
        excess = (last_close - upper_band) / band_width
        confidence = float(min(0.5 + excess, 1.0))
        stop = upper_band * (1 + STOP_BEYOND_PCT)
        return {
            "signal": "SELL",
            "confidence": confidence,
            "reason": (f"price ${last_close:.2f} above upper VWAP band "
                       f"${upper_band:.2f}; RSI {last_rsi:.1f} > {RSI_OVERBOUGHT}"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(stop),
            "take_profit": last_vwap,
        }

    return _hold(
        f"price ${last_close:.2f} inside bands [${lower_band:.2f},${upper_band:.2f}], "
        f"RSI {last_rsi:.1f}",
        symbol,
    )


def _hold(reason: str, symbol: str) -> dict[str, Any]:
    return {
        "signal": "HOLD",
        "confidence": 0.0,
        "reason": reason,
        "strategy": NAME,
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
    }
