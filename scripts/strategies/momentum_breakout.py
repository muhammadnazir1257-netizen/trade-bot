"""Opening Range Breakout (ORB) momentum strategy.

Captures the first 30 minutes of trading (9:30–10:00 ET) and trades the
breakout above/below that range with volume + ADX trend confirmation.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
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

NAME = "momentum_breakout"
VOLUME_MULT_TRIGGER = 1.5
TAKE_PROFIT_MULT = 2.0   # 2× OR size projected from breakout


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Trade the Opening Range break with ADX + volume confirmation."""
    if not bars_5m or len(bars_5m) < 8:  # need enough for OR + ADX
        return _hold("insufficient_5m_bars", symbol)

    or_high, or_low, or_bars_used = _opening_range(bars_5m)
    if or_high is None:
        return _hold("no_opening_range_data", symbol)

    opens, highs, lows, closes, vols = ind.extract_ohlcv(bars_5m)
    adx_arr = ind.adx(highs, lows, closes, period=config.ADX_PERIOD)
    last_adx = float(adx_arr[-1]) if not np.isnan(adx_arr[-1]) else 0.0

    if last_adx < config.ADX_TREND_THRESHOLD:
        return _hold(f"ADX {last_adx:.1f} < {config.ADX_TREND_THRESHOLD}; market not trending", symbol)

    last_close = float(closes[-1])
    last_vol = float(vols[-1])
    # Volume baseline = average of bars excluding the OR and the current bar
    if len(vols) <= or_bars_used + 1:
        return _hold("not_enough_post_OR_bars", symbol)
    vol_baseline = float(np.mean(vols[or_bars_used:-1])) if or_bars_used < len(vols) - 1 else last_vol
    if vol_baseline <= 0:
        return _hold("no_volume_baseline", symbol)
    volume_ratio = last_vol / vol_baseline

    if volume_ratio < VOLUME_MULT_TRIGGER:
        return _hold(
            f"volume ratio {volume_ratio:.2f}× < {VOLUME_MULT_TRIGGER}× threshold",
            symbol,
        )

    # Confidence: volume excess × ADX strength (normalized)
    vol_score = min((volume_ratio - VOLUME_MULT_TRIGGER) / VOLUME_MULT_TRIGGER, 1.0)
    adx_score = min((last_adx - config.ADX_TREND_THRESHOLD) / 30.0, 1.0)
    confidence = float(max(0.4, min(0.4 + 0.6 * vol_score * adx_score + 0.2 * adx_score, 1.0)))

    or_size = or_high - or_low

    if last_close > or_high:
        return {
            "signal": "BUY",
            "confidence": confidence,
            "reason": (f"close ${last_close:.2f} > OR high ${or_high:.2f}; "
                       f"vol {volume_ratio:.2f}×; ADX {last_adx:.1f}"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(or_low),                       # back inside the range
            "take_profit": float(or_high + TAKE_PROFIT_MULT * or_size),
        }
    if last_close < or_low:
        return {
            "signal": "SELL",
            "confidence": confidence,
            "reason": (f"close ${last_close:.2f} < OR low ${or_low:.2f}; "
                       f"vol {volume_ratio:.2f}×; ADX {last_adx:.1f}"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(or_high),
            "take_profit": float(or_low - TAKE_PROFIT_MULT * or_size),
        }

    return _hold(
        f"close ${last_close:.2f} inside OR [${or_low:.2f},${or_high:.2f}]",
        symbol,
    )


def _opening_range(bars_5m: list) -> tuple[float | None, float | None, int]:
    """Find today's first ORB_MINUTES of 5-minute bars and return their high/low.

    Returns (or_high, or_low, num_bars_used). Uses the first N consecutive
    bars from the most recent trading session in the input list. N is
    ``ORB_MINUTES // 5`` (e.g. 6 bars for the default 30-minute opening range).
    """
    if not bars_5m:
        return None, None, 0

    bars_needed = max(1, config.ORB_MINUTES // 5)

    # Find boundary: the last bar with timestamp on a different ET date than
    # the most recent bar. Everything after that boundary belongs to the
    # current session. We approximate "trading day" by UTC date here since
    # for our scheduled times the UTC date == ET date.
    try:
        last_date = _bar_date(bars_5m[-1])
    except Exception:
        last_date = None

    session_bars = []
    if last_date is None:
        session_bars = bars_5m[:bars_needed]
    else:
        for b in bars_5m:
            try:
                if _bar_date(b) == last_date:
                    session_bars.append(b)
            except Exception:
                continue
        session_bars = session_bars[:bars_needed]

    if len(session_bars) < bars_needed:
        return None, None, 0
    or_high = float(max(b["h"] for b in session_bars))
    or_low = float(min(b["l"] for b in session_bars))
    return or_high, or_low, bars_needed


def _bar_date(bar: dict) -> str:
    """Extract YYYY-MM-DD from a bar's ``t`` field."""
    t = bar.get("t", "")
    if isinstance(t, str) and len(t) >= 10:
        return t[:10]
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
    return ""


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
