"""Dynamic Support / Resistance + candlestick price-action strategy.

Builds S/R levels from 60 days of daily bars using a zigzag swing detector,
scores them by touches × recency × relative volume, then looks for
candlestick reversal patterns when price reaches a top-3 level.

Detected candlestick patterns:
  - Hammer (bullish), Shooting Star (bearish)
  - Bullish Engulfing, Bearish Engulfing
  - Pin Bar (long-wick rejection, direction set by wick side)
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

NAME = "support_resistance"
LOOKBACK_DAYS = 60
ZIGZAG_PCT = 0.03
PROXIMITY_PCT = 0.005   # within 0.5% of a level counts as "at the level"
STOP_BUFFER_PCT = 0.003


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Trade reversal price-action off the nearest scored S/R level."""
    if not bars_1d or len(bars_1d) < 10:
        return _hold("insufficient_daily_bars", symbol)

    daily = bars_1d[-LOOKBACK_DAYS:]
    opens, highs, lows, closes, vols = ind.extract_ohlcv(daily)
    pivots = ind.zigzag(closes.tolist(), threshold_pct=ZIGZAG_PCT)
    if not pivots:
        return _hold("no_zigzag_pivots", symbol)

    levels = _score_levels(pivots, highs, lows, vols)
    if not levels:
        return _hold("no_scored_levels", symbol)

    last_close = float(closes[-1])
    supports = sorted([lv for lv in levels if lv["price"] < last_close],
                      key=lambda x: x["score"], reverse=True)[:3]
    resistances = sorted([lv for lv in levels if lv["price"] > last_close],
                         key=lambda x: x["score"], reverse=True)[:3]

    pattern = _detect_pattern(opens, highs, lows, closes)
    if pattern["pattern"] == "none":
        return _hold(
            f"close ${last_close:.2f}; no reversal pattern detected on recent daily bars",
            symbol,
        )

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    # BUY: at support with a bullish reversal pattern
    if nearest_support and pattern["direction"] == "bullish":
        if abs(last_close - nearest_support["price"]) / last_close <= PROXIMITY_PCT:
            confidence = float(max(0.4, min(0.4 + 0.4 * pattern["strength"]
                                            + 0.1 * min(nearest_support["score"], 5) / 5, 1.0)))
            stop = nearest_support["price"] * (1 - STOP_BUFFER_PCT)
            tp = nearest_resistance["price"] if nearest_resistance else last_close * 1.05
            return {
                "signal": "BUY",
                "confidence": confidence,
                "reason": (f"{pattern['pattern']} pattern at support ${nearest_support['price']:.2f} "
                           f"(score {nearest_support['score']:.1f}); close ${last_close:.2f}"),
                "strategy": NAME,
                "entry_price": last_close,
                "stop_loss": float(stop),
                "take_profit": float(tp),
            }

    # SELL: at resistance with a bearish reversal pattern
    if nearest_resistance and pattern["direction"] == "bearish":
        if abs(last_close - nearest_resistance["price"]) / last_close <= PROXIMITY_PCT:
            confidence = float(max(0.4, min(0.4 + 0.4 * pattern["strength"]
                                            + 0.1 * min(nearest_resistance["score"], 5) / 5, 1.0)))
            stop = nearest_resistance["price"] * (1 + STOP_BUFFER_PCT)
            tp = nearest_support["price"] if nearest_support else last_close * 0.95
            return {
                "signal": "SELL",
                "confidence": confidence,
                "reason": (f"{pattern['pattern']} pattern at resistance "
                           f"${nearest_resistance['price']:.2f} (score "
                           f"{nearest_resistance['score']:.1f}); close ${last_close:.2f}"),
                "strategy": NAME,
                "entry_price": last_close,
                "stop_loss": float(stop),
                "take_profit": float(tp),
            }

    return _hold(
        f"{pattern['pattern']} detected but not at scored level (close ${last_close:.2f})",
        symbol,
    )


def _score_levels(pivots: list[dict], highs: np.ndarray, lows: np.ndarray,
                  vols: np.ndarray) -> list[dict]:
    """Group nearby pivots into levels and score them.

    Score = touches × recency_weight × volume_weight.
    """
    if not pivots:
        return []
    total_bars = len(highs)
    cluster_pct = 0.015
    clusters: list[dict] = []
    for piv in pivots:
        merged = False
        for cl in clusters:
            if abs(cl["price"] - piv["price"]) / cl["price"] <= cluster_pct:
                cl["touches"] += 1
                cl["last_index"] = max(cl["last_index"], piv["index"])
                cl["volume_sum"] += float(vols[piv["index"]]) if piv["index"] < len(vols) else 0.0
                cl["price"] = (cl["price"] * (cl["touches"] - 1) + piv["price"]) / cl["touches"]
                merged = True
                break
        if not merged:
            clusters.append({
                "price": float(piv["price"]),
                "touches": 1,
                "last_index": int(piv["index"]),
                "volume_sum": float(vols[piv["index"]]) if piv["index"] < len(vols) else 0.0,
                "type": piv["type"],
            })
    avg_vol = float(np.mean(vols)) if len(vols) > 0 else 1.0
    if avg_vol == 0:
        avg_vol = 1.0
    for cl in clusters:
        recency = 1.0 - (total_bars - 1 - cl["last_index"]) / max(total_bars - 1, 1)
        vol_per_touch = cl["volume_sum"] / cl["touches"]
        vol_weight = vol_per_touch / avg_vol
        cl["score"] = cl["touches"] * (0.5 + 0.5 * recency) * (0.5 + 0.5 * min(vol_weight, 3.0))
    return clusters


def _detect_pattern(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                    closes: np.ndarray) -> dict:
    """Detect a single candlestick reversal pattern on the last 1–2 bars.

    Returns ``{"pattern", "direction", "strength"}``. ``strength`` is 0–1.
    """
    if len(closes) < 2:
        return {"pattern": "none", "direction": "neutral", "strength": 0.0}

    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    o_prev, c_prev = opens[-2], closes[-2]
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return {"pattern": "none", "direction": "neutral", "strength": 0.0}
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_pct = body / rng

    # Hammer: small body in upper half, long lower wick
    if lower_wick > 2 * body and upper_wick < body and body_pct < 0.35 and c > o:
        return {"pattern": "hammer", "direction": "bullish",
                "strength": float(min(lower_wick / rng, 1.0))}
    # Shooting star: small body in lower half, long upper wick
    if upper_wick > 2 * body and lower_wick < body and body_pct < 0.35 and c < o:
        return {"pattern": "shooting_star", "direction": "bearish",
                "strength": float(min(upper_wick / rng, 1.0))}
    # Bullish engulfing
    if c_prev < o_prev and c > o and o < c_prev and c > o_prev:
        return {"pattern": "bullish_engulfing", "direction": "bullish",
                "strength": float(min(body / max(abs(c_prev - o_prev), 1e-6) - 1.0, 1.0))}
    # Bearish engulfing
    if c_prev > o_prev and c < o and o > c_prev and c < o_prev:
        return {"pattern": "bearish_engulfing", "direction": "bearish",
                "strength": float(min(body / max(abs(c_prev - o_prev), 1e-6) - 1.0, 1.0))}
    # Pin bar — long wick on one side, small body
    if body_pct < 0.3:
        if lower_wick > 0.6 * rng:
            return {"pattern": "bullish_pin_bar", "direction": "bullish",
                    "strength": float(lower_wick / rng)}
        if upper_wick > 0.6 * rng:
            return {"pattern": "bearish_pin_bar", "direction": "bearish",
                    "strength": float(upper_wick / rng)}
    return {"pattern": "none", "direction": "neutral", "strength": 0.0}


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
