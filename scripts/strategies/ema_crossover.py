"""Multi-EMA crossover trend-following strategy.

BUY: EMA8 crosses above EMA21 while EMA21 > EMA55 (bullish alignment),
     confirmed by MACD histogram > 0.
SELL: EMA8 crosses below EMA21 while EMA21 < EMA55, MACD histogram < 0.

Runs on 5-minute bars. Stop = EMA55; take-profit = 3:1 reward-to-risk from entry.
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

NAME = "ema_crossover"


def analyze(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
            account: dict, positions: dict) -> dict[str, Any]:
    """Detect EMA8/21 crossover with EMA55 trend filter + MACD confirmation."""
    if not bars_5m or len(bars_5m) < config.EMA_SLOW + config.MACD_SLOW + 5:
        return _hold("insufficient_5m_bars", symbol)

    _, _, _, closes, _ = ind.extract_ohlcv(bars_5m)

    ema_f = ind.ema(closes, config.EMA_FAST)
    ema_m = ind.ema(closes, config.EMA_MID)
    ema_s = ind.ema(closes, config.EMA_SLOW)
    macd_line, sig_line, hist = ind.macd(
        closes, fast=config.MACD_FAST, slow=config.MACD_SLOW, signal=config.MACD_SIGNAL
    )

    if np.isnan(ema_f[-1]) or np.isnan(ema_m[-1]) or np.isnan(ema_s[-1]):
        return _hold("emas_not_warm", symbol)
    if np.isnan(hist[-1]) or np.isnan(hist[-2]):
        return _hold("macd_not_warm", symbol)

    last_close = float(closes[-1])
    f_prev, f_now = float(ema_f[-2]), float(ema_f[-1])
    m_prev, m_now = float(ema_m[-2]), float(ema_m[-1])
    s_now = float(ema_s[-1])
    h_now = float(hist[-1])

    bullish_cross = f_prev <= m_prev and f_now > m_now
    bearish_cross = f_prev >= m_prev and f_now < m_now
    bullish_align = m_now > s_now
    bearish_align = m_now < s_now

    # Distance of EMAs from each other as % of price → trend strength input
    ema_spread_pct = abs(f_now - m_now) / last_close if last_close > 0 else 0.0
    # Normalize MACD histogram by recent price range so values are bounded
    recent_atr_arr = ind.atr(*ind.extract_ohlcv(bars_5m)[1:4], period=config.ATR_PERIOD)
    atr_now = float(recent_atr_arr[-1]) if not np.isnan(recent_atr_arr[-1]) else last_close * 0.01
    hist_norm = min(abs(h_now) / atr_now, 1.0) if atr_now > 0 else 0.0
    confidence = float(max(0.4, min(0.4 + 50 * ema_spread_pct + 0.4 * hist_norm, 1.0)))

    if bullish_cross and bullish_align and h_now > 0:
        stop = s_now
        risk = last_close - stop
        if risk <= 0:
            return _hold("invalid_risk_distance_for_buy", symbol)
        take_profit = last_close + 3.0 * risk
        return {
            "signal": "BUY",
            "confidence": confidence,
            "reason": (f"EMA{config.EMA_FAST} crossed above EMA{config.EMA_MID}; "
                       f"EMA{config.EMA_MID} > EMA{config.EMA_SLOW}; MACD hist {h_now:.3f}>0"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(stop),
            "take_profit": float(take_profit),
        }
    if bearish_cross and bearish_align and h_now < 0:
        stop = s_now
        risk = stop - last_close
        if risk <= 0:
            return _hold("invalid_risk_distance_for_sell", symbol)
        take_profit = last_close - 3.0 * risk
        return {
            "signal": "SELL",
            "confidence": confidence,
            "reason": (f"EMA{config.EMA_FAST} crossed below EMA{config.EMA_MID}; "
                       f"EMA{config.EMA_MID} < EMA{config.EMA_SLOW}; MACD hist {h_now:.3f}<0"),
            "strategy": NAME,
            "entry_price": last_close,
            "stop_loss": float(stop),
            "take_profit": float(take_profit),
        }

    return _hold(
        f"no aligned cross (EMA8={f_now:.2f} EMA21={m_now:.2f} EMA55={s_now:.2f} hist={h_now:.3f})",
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
