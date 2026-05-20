"""TTM Squeeze (Bollinger Bands inside Keltner Channels) momentum strategy.

Squeeze ON: Bollinger Bands (20, 2.0) are contained within Keltner Channels
(20, 1.5×ATR) — volatility compression.
Squeeze FIRES: first bar where BBs expand back outside KCs.
Direction set by a momentum oscillator (close minus the midpoint of the
20-period range, smoothed).
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

NAME = "squeeze_momentum"
MIN_SQUEEZE_DURATION = 4   # bars of compression required to consider the fire significant


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Detect a squeeze fire and trade the implied momentum direction."""
    if not bars_5m or len(bars_5m) < config.BB_PERIOD + 5:
        return _hold("insufficient_5m_bars", symbol)

    _, highs, lows, closes, _ = ind.extract_ohlcv(bars_5m)
    bb_u, bb_m, bb_l = ind.bollinger_bands(closes, period=config.BB_PERIOD, std_dev=config.BB_STDEV)
    kc_u, kc_m, kc_l = ind.keltner_channels(highs, lows, closes,
                                            period=config.KC_PERIOD,
                                            atr_mult=config.KC_ATR_MULT)
    if np.isnan(bb_u[-1]) or np.isnan(kc_u[-1]):
        return _hold("bands_not_warm", symbol)

    n = len(closes)
    # Per-bar squeeze flag
    squeeze = np.full(n, False)
    for i in range(n):
        if np.isnan(bb_u[i]) or np.isnan(kc_u[i]):
            continue
        squeeze[i] = (bb_u[i] < kc_u[i]) and (bb_l[i] > kc_l[i])

    # Find squeeze fire — current bar NOT in squeeze but previous WAS
    if squeeze[-1] or not squeeze[-2]:
        return _hold(f"no squeeze fire (now={squeeze[-1]} prev={squeeze[-2]})", symbol)

    # Duration of the squeeze that just ended
    duration = 0
    for i in range(n - 2, -1, -1):
        if squeeze[i]:
            duration += 1
        else:
            break
    if duration < MIN_SQUEEZE_DURATION:
        return _hold(f"squeeze too short ({duration} bars)", symbol)

    # Momentum oscillator: close minus average of (highest high + lowest low + SMA20)/3
    hist_window = 20
    if n < hist_window:
        return _hold("insufficient_history_for_momentum", symbol)
    highest = float(np.max(highs[-hist_window:]))
    lowest = float(np.min(lows[-hist_window:]))
    sma20_arr = ind.sma(closes, hist_window)
    if np.isnan(sma20_arr[-1]):
        return _hold("sma_not_warm", symbol)
    avg = (highest + lowest + float(sma20_arr[-1])) / 3.0
    momentum_now = float(closes[-1]) - avg
    momentum_prev = float(closes[-2]) - avg
    rising = momentum_now > momentum_prev

    confidence = float(max(0.4, min(0.4 + 0.05 * duration, 1.0)))
    last_close = float(closes[-1])
    kc_width = float(kc_u[-1] - kc_l[-1])

    if momentum_now > 0 and rising:
        return {
            "signal": "BUY",
            "confidence": confidence,
            "reason": (f"squeeze fired after {duration} bars; momentum +{momentum_now:.3f}, "
                       f"rising"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(kc_l[-1]),
            "take_profit": float(last_close + 2 * kc_width),
        }
    if momentum_now < 0 and not rising:
        return {
            "signal": "SELL",
            "confidence": confidence,
            "reason": (f"squeeze fired after {duration} bars; momentum {momentum_now:.3f}, "
                       f"falling"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(kc_u[-1]),
            "take_profit": float(last_close - 2 * kc_width),
        }

    return _hold(
        f"squeeze fired but momentum ambiguous ({momentum_now:.3f}, rising={rising})",
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
