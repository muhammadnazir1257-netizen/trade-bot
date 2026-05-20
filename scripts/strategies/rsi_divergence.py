"""RSI Divergence reversal strategy.

Bullish divergence: price prints a lower low while RSI prints a higher low
  → mean-reversion BUY.
Bearish divergence: price prints a higher high while RSI prints a lower high
  → mean-reversion SELL.

Divergence must span >= 5 bars and the recent bar's volume must exceed 1.2×
the 20-bar average for confirmation.
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

NAME = "rsi_divergence"
MIN_DIVERGENCE_SPAN = 5
VOLUME_CONFIRM_MULT = 1.2
LOOKBACK = 30


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Detect RSI/price divergences on 5m bars (with 1d corroboration)."""
    if not bars_5m or len(bars_5m) < LOOKBACK + config.RSI_PERIOD + 2:
        return _hold("insufficient_5m_bars", symbol)

    _, highs, lows, closes, vols = ind.extract_ohlcv(bars_5m)
    rsi_arr = ind.rsi(closes, period=config.RSI_PERIOD)
    if np.isnan(rsi_arr[-1]):
        return _hold("rsi_not_warm", symbol)

    window_low = lows[-LOOKBACK:]
    window_high = highs[-LOOKBACK:]
    window_rsi = rsi_arr[-LOOKBACK:]
    window_vol = vols[-LOOKBACK:]
    valid = ~np.isnan(window_rsi)
    if valid.sum() < MIN_DIVERGENCE_SPAN + 2:
        return _hold("not_enough_valid_rsi_bars", symbol)

    last_close = float(closes[-1])

    # Find the most recent pivot low/high and the prior one
    recent_low_idx, prev_low_idx = _two_lowest(window_low, valid)
    recent_high_idx, prev_high_idx = _two_highest(window_high, valid)

    # Volume confirmation: current bar vs 20-bar average
    vol_avg = float(np.mean(vols[-20:]))
    last_vol = float(vols[-1])
    vol_ok = vol_avg > 0 and last_vol >= VOLUME_CONFIRM_MULT * vol_avg

    # Bullish divergence
    if recent_low_idx is not None and prev_low_idx is not None \
            and (recent_low_idx - prev_low_idx) >= MIN_DIVERGENCE_SPAN:
        price_lower_low = window_low[recent_low_idx] < window_low[prev_low_idx]
        rsi_higher_low = window_rsi[recent_low_idx] > window_rsi[prev_low_idx]
        if price_lower_low and rsi_higher_low and vol_ok:
            angle = (window_rsi[recent_low_idx] - window_rsi[prev_low_idx]) / max(
                1.0, recent_low_idx - prev_low_idx)
            confidence = float(max(0.4, min(0.4 + 0.05 * angle + 0.2, 1.0)))
            pivot_low = float(window_low[recent_low_idx])
            swing_top = float(np.max(window_high[prev_low_idx:recent_low_idx + 1]))
            take_profit = pivot_low + 0.5 * (swing_top - pivot_low)
            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (f"bullish divergence: price LL @ ${window_low[recent_low_idx]:.2f} "
                           f"vs prior LL ${window_low[prev_low_idx]:.2f}; RSI HL "
                           f"({window_rsi[prev_low_idx]:.1f}→{window_rsi[recent_low_idx]:.1f}); "
                           f"vol {last_vol/vol_avg:.2f}×"),
                "strategy": NAME,
                "entry_price": last_close,
                "stop_loss": pivot_low * 0.997,
                "take_profit": float(take_profit),
            }

    # Bearish divergence
    if recent_high_idx is not None and prev_high_idx is not None \
            and (recent_high_idx - prev_high_idx) >= MIN_DIVERGENCE_SPAN:
        price_higher_high = window_high[recent_high_idx] > window_high[prev_high_idx]
        rsi_lower_high = window_rsi[recent_high_idx] < window_rsi[prev_high_idx]
        if price_higher_high and rsi_lower_high and vol_ok:
            angle = (window_rsi[prev_high_idx] - window_rsi[recent_high_idx]) / max(
                1.0, recent_high_idx - prev_high_idx)
            confidence = float(max(0.4, min(0.4 + 0.05 * angle + 0.2, 1.0)))
            pivot_high = float(window_high[recent_high_idx])
            swing_bottom = float(np.min(window_low[prev_high_idx:recent_high_idx + 1]))
            take_profit = pivot_high - 0.5 * (pivot_high - swing_bottom)
            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": (f"bearish divergence: price HH @ ${window_high[recent_high_idx]:.2f} "
                           f"vs prior HH ${window_high[prev_high_idx]:.2f}; RSI LH "
                           f"({window_rsi[prev_high_idx]:.1f}→{window_rsi[recent_high_idx]:.1f}); "
                           f"vol {last_vol/vol_avg:.2f}×"),
                "strategy": NAME,
                "entry_price": last_close,
                "stop_loss": pivot_high * 1.003,
                "take_profit": float(take_profit),
            }

    return _hold("no_qualifying_divergence", symbol)


def _two_lowest(arr: np.ndarray, valid_mask: np.ndarray):
    """Return (most_recent_low_idx, prior_low_idx) within ``arr`` (local minima
    seen looking back). Indices are in the windowed array."""
    n = len(arr)
    if n < 3:
        return None, None
    # Find local minima where bar is lower than its immediate neighbours
    minima = []
    for i in range(1, n - 1):
        if not valid_mask[i]:
            continue
        if arr[i] <= arr[i - 1] and arr[i] <= arr[i + 1]:
            minima.append(i)
    if len(minima) < 2:
        return None, None
    return minima[-1], minima[-2]


def _two_highest(arr: np.ndarray, valid_mask: np.ndarray):
    """Mirror of :func:`_two_lowest` for local maxima."""
    n = len(arr)
    if n < 3:
        return None, None
    maxima = []
    for i in range(1, n - 1):
        if not valid_mask[i]:
            continue
        if arr[i] >= arr[i - 1] and arr[i] >= arr[i + 1]:
            maxima.append(i)
    if len(maxima) < 2:
        return None, None
    return maxima[-1], maxima[-2]


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
