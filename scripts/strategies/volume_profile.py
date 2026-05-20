"""Volume Profile / Point-of-Control strategy.

Builds today's volume-by-price histogram from 1-minute bars to identify:
- POC (Point of Control): price bin with the highest cumulative volume
- VAH / VAL (Value Area High/Low): tightest range containing 70% of volume

Signals:
- BUY: price pulls back to VAL from above with decreasing sell-side volume
- SELL: price pushes to VAH from below with decreasing buy-side volume
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

NAME = "volume_profile"
PROXIMITY_PCT = 0.003   # within 0.3% of VAL/VAH counts as "at the level"


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Volume-profile mean-reversion / value-area edge trade."""
    if not bars_1m or len(bars_1m) < 60:
        return _hold("insufficient_1m_bars", symbol)

    opens, highs, lows, closes, vols = ind.extract_ohlcv(bars_1m)
    if float(np.sum(vols)) <= 0:
        return _hold("zero_session_volume", symbol)

    poc, vah, val, vol_at_poc, total_vol = _build_profile(highs, lows, closes, vols)
    if poc is None:
        return _hold("could_not_build_profile", symbol)

    last_close = float(closes[-1])
    # Sense of volume direction: average volume on recent up vs down bars
    recent_up_vol, recent_down_vol = _recent_directional_volume(opens, closes, vols, n=10)

    near_val = abs(last_close - val) / max(val, 1e-6) <= PROXIMITY_PCT
    near_vah = abs(last_close - vah) / max(vah, 1e-6) <= PROXIMITY_PCT

    poc_share = vol_at_poc / total_vol if total_vol > 0 else 0.0

    if near_val and last_close >= val and recent_down_vol < recent_up_vol * 1.1:
        # Sell-pressure slowing into value area — long the rejection
        confidence = float(max(0.4, min(0.4 + 2 * poc_share + 0.2, 1.0)))
        return {
            "signal": "BUY",
            "confidence": confidence,
            "reason": (f"price ${last_close:.2f} at VAL ${val:.2f}; sell-vol slowing "
                       f"(down/up recent vol ratio {recent_down_vol/max(recent_up_vol,1e-6):.2f}); "
                       f"POC ${poc:.2f} share {poc_share:.1%}"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(min(val, poc) * 0.997),   # beyond POC
            "take_profit": float(vah),                   # opposite VA boundary
        }
    if near_vah and last_close <= vah and recent_up_vol < recent_down_vol * 1.1:
        confidence = float(max(0.4, min(0.4 + 2 * poc_share + 0.2, 1.0)))
        return {
            "signal": "SELL",
            "confidence": confidence,
            "reason": (f"price ${last_close:.2f} at VAH ${vah:.2f}; buy-vol slowing "
                       f"(up/down recent vol ratio {recent_up_vol/max(recent_down_vol,1e-6):.2f}); "
                       f"POC ${poc:.2f} share {poc_share:.1%}"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(max(vah, poc) * 1.003),
            "take_profit": float(val),
        }

    return _hold(
        f"price ${last_close:.2f} not at VAL ${val:.2f} or VAH ${vah:.2f} "
        f"(POC ${poc:.2f})",
        symbol,
    )


def _build_profile(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                   vols: np.ndarray):
    """Bin volume across the day's price range. Returns
    ``(poc_price, vah, val, vol_at_poc, total_vol)``.
    """
    price_min = float(np.min(lows))
    price_max = float(np.max(highs))
    if price_max <= price_min:
        return None, None, None, 0.0, 0.0

    n_bins = config.VP_BIN_COUNT
    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_volume = np.zeros(n_bins)
    typical = (highs + lows + closes) / 3.0
    for tp, v in zip(typical, vols):
        # locate bin for typical price
        idx = int(min(n_bins - 1, max(0, (tp - price_min) / (price_max - price_min) * n_bins)))
        bin_volume[idx] += v

    total = float(np.sum(bin_volume))
    if total <= 0:
        return None, None, None, 0.0, 0.0

    poc_idx = int(np.argmax(bin_volume))
    poc_price = float((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2.0)
    vol_at_poc = float(bin_volume[poc_idx])

    # Expand around POC until we capture VP_VALUE_AREA_PCT of total volume
    target = config.VP_VALUE_AREA_PCT * total
    captured = vol_at_poc
    lo_idx = hi_idx = poc_idx
    while captured < target and (lo_idx > 0 or hi_idx < n_bins - 1):
        next_lo = bin_volume[lo_idx - 1] if lo_idx > 0 else -1
        next_hi = bin_volume[hi_idx + 1] if hi_idx < n_bins - 1 else -1
        if next_hi >= next_lo:
            hi_idx += 1
            captured += next_hi
        else:
            lo_idx -= 1
            captured += next_lo

    val = float(bin_edges[lo_idx])
    vah = float(bin_edges[hi_idx + 1])
    return poc_price, vah, val, vol_at_poc, total


def _recent_directional_volume(opens: np.ndarray, closes: np.ndarray,
                               vols: np.ndarray, n: int = 10):
    """Sum volume on bars that closed higher vs lower over the last ``n`` bars."""
    if len(opens) < n:
        n = len(opens)
    o = opens[-n:]
    c = closes[-n:]
    v = vols[-n:]
    up_vol = float(np.sum(v[c > o]))
    down_vol = float(np.sum(v[c < o]))
    return up_vol, down_vol


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
