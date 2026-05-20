"""Pure-Python / numpy technical indicator library.

All indicators take plain Python lists / numpy arrays of OHLCV values and
return numpy arrays the same length as the input. Index alignment is
preserved: the first ``period - 1`` entries (or however many can't be
computed) are ``NaN`` so callers can use ``-1`` indexing and ignore leading
NaNs.

No dependency on TA-Lib or pandas — only numpy.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

NaN = float("nan")
ArrayLike = Sequence[float]


# --- Helpers ---------------------------------------------------------------


def _as_array(x: ArrayLike) -> np.ndarray:
    """Convert any sequence to a 1-D float numpy array."""
    return np.asarray(x, dtype=float)


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing — exponential with alpha = 1/period.

    First valid value is the simple sum (Welles Wilder's original convention).
    """
    out = np.full(values.shape, NaN)
    if len(values) < period:
        return out
    out[period - 1] = np.nansum(values[:period])
    for i in range(period, len(values)):
        out[i] = out[i - 1] - (out[i - 1] / period) + values[i]
    return out


# --- Moving averages -------------------------------------------------------


def sma(prices: ArrayLike, period: int) -> np.ndarray:
    """Simple Moving Average."""
    p = _as_array(prices)
    out = np.full(p.shape, NaN)
    if len(p) < period or period <= 0:
        return out
    # Use cumulative trick for O(n) SMA
    csum = np.cumsum(p)
    out[period - 1] = csum[period - 1] / period
    out[period:] = (csum[period:] - csum[:-period]) / period
    return out


def ema(prices: ArrayLike, period: int) -> np.ndarray:
    """Exponential Moving Average. Bootstraps with SMA of first ``period``."""
    p = _as_array(prices)
    out = np.full(p.shape, NaN)
    if len(p) < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = float(np.mean(p[:period]))
    for i in range(period, len(p)):
        out[i] = p[i] * alpha + out[i - 1] * (1 - alpha)
    return out


# --- Momentum oscillators --------------------------------------------------


def rsi(prices: ArrayLike, period: int = 14) -> np.ndarray:
    """Relative Strength Index using Wilder smoothing."""
    p = _as_array(prices)
    out = np.full(p.shape, NaN)
    if len(p) <= period:
        return out
    deltas = np.diff(p)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, len(p)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(prices: ArrayLike, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram. Returns (macd, signal, hist)."""
    p = _as_array(prices)
    ema_fast = ema(p, fast)
    ema_slow = ema(p, slow)
    macd_line = ema_fast - ema_slow
    # Signal is EMA of the macd line; substitute NaNs with 0 for the EMA
    macd_clean = np.where(np.isnan(macd_line), 0.0, macd_line)
    sig_line = ema(macd_clean, signal)
    # But re-mask the leading NaNs from macd_line
    leading_nan = np.isnan(macd_line)
    sig_line = np.where(leading_nan, NaN, sig_line)
    hist = macd_line - sig_line
    return macd_line, sig_line, hist


def stochastic(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
               k: int = 14, d: int = 3):
    """Stochastic %K and %D oscillator. Returns (%K, %D)."""
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    k_arr = np.full(c.shape, NaN)
    if len(c) < k:
        return k_arr, k_arr.copy()
    for i in range(k - 1, len(c)):
        lo = float(np.min(l[i - k + 1:i + 1]))
        hi = float(np.max(h[i - k + 1:i + 1]))
        rng = hi - lo
        k_arr[i] = 100.0 * (c[i] - lo) / rng if rng > 0 else 50.0
    d_arr = sma(k_arr, d)
    return k_arr, d_arr


# --- Volatility ------------------------------------------------------------


def atr(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
        period: int = 14) -> np.ndarray:
    """Average True Range (Wilder-smoothed)."""
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    n = len(c)
    out = np.full(n, NaN)
    if n < period + 1:
        return out
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    # First ATR is simple average of first `period` TRs
    out[period] = float(np.mean(tr[1:period + 1]))
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger_bands(prices: ArrayLike, period: int = 20, std_dev: float = 2.0):
    """Bollinger Bands. Returns (upper, middle, lower)."""
    p = _as_array(prices)
    middle = sma(p, period)
    n = len(p)
    upper = np.full(n, NaN)
    lower = np.full(n, NaN)
    if n < period:
        return upper, middle, lower
    for i in range(period - 1, n):
        window = p[i - period + 1:i + 1]
        sd = float(np.std(window, ddof=0))
        upper[i] = middle[i] + std_dev * sd
        lower[i] = middle[i] - std_dev * sd
    return upper, middle, lower


def keltner_channels(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
                     period: int = 20, atr_mult: float = 1.5):
    """Keltner Channels — EMA middle ± atr_mult × ATR. Returns (upper, middle, lower)."""
    c = _as_array(closes)
    middle = ema(c, period)
    atr_vals = atr(highs, lows, closes, period)
    upper = middle + atr_mult * atr_vals
    lower = middle - atr_mult * atr_vals
    return upper, middle, lower


# --- Trend strength --------------------------------------------------------


def adx(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
        period: int = 14) -> np.ndarray:
    """Average Directional Index (Wilder)."""
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    n = len(c)
    out = np.full(n, NaN)
    if n < 2 * period:
        return out

    up_move = np.diff(h)
    down_move = -np.diff(l)
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.zeros(n - 1)
    for i in range(1, n):
        tr[i - 1] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

    # Smooth via Wilder
    smoothed_tr = np.zeros(n - 1)
    smoothed_plus = np.zeros(n - 1)
    smoothed_minus = np.zeros(n - 1)
    smoothed_tr[period - 1] = float(np.sum(tr[:period]))
    smoothed_plus[period - 1] = float(np.sum(plus_dm[:period]))
    smoothed_minus[period - 1] = float(np.sum(minus_dm[:period]))
    for i in range(period, n - 1):
        smoothed_tr[i] = smoothed_tr[i - 1] - smoothed_tr[i - 1] / period + tr[i]
        smoothed_plus[i] = smoothed_plus[i - 1] - smoothed_plus[i - 1] / period + plus_dm[i]
        smoothed_minus[i] = smoothed_minus[i - 1] - smoothed_minus[i - 1] / period + minus_dm[i]

    dx = np.full(n - 1, NaN)
    for i in range(period - 1, n - 1):
        if smoothed_tr[i] == 0:
            continue
        di_plus = 100.0 * smoothed_plus[i] / smoothed_tr[i]
        di_minus = 100.0 * smoothed_minus[i] / smoothed_tr[i]
        denom = di_plus + di_minus
        dx[i] = 100.0 * abs(di_plus - di_minus) / denom if denom > 0 else 0.0

    # ADX is Wilder smoothing of DX
    if n - 1 < 2 * period - 1:
        return out
    first_idx = 2 * period - 2  # first index where ADX is defined
    valid_dx = dx[period - 1:first_idx + 1]
    valid_dx = valid_dx[~np.isnan(valid_dx)]
    if len(valid_dx) == 0:
        return out
    out[first_idx + 1] = float(np.mean(valid_dx))
    for i in range(first_idx + 2, n):
        if np.isnan(dx[i - 1]):
            out[i] = out[i - 1]
        else:
            out[i] = (out[i - 1] * (period - 1) + dx[i - 1]) / period
    return out


# --- Volume / price metrics ------------------------------------------------


def vwap(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
         volumes: ArrayLike) -> np.ndarray:
    """Volume-Weighted Average Price (cumulative, intraday-style)."""
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    v = _as_array(volumes)
    typical = (h + l + c) / 3.0
    cum_vp = np.cumsum(typical * v)
    cum_v = np.cumsum(v)
    # Guard against zero volume
    out = np.where(cum_v > 0, cum_vp / np.where(cum_v == 0, 1, cum_v), NaN)
    return out


# --- Pivots & swings -------------------------------------------------------


def pivot_points(high: float, low: float, close: float) -> dict:
    """Classic pivot points from a single bar (typically prior day's HLC).

    Returns ``{"P", "R1", "R2", "R3", "S1", "S2", "S3"}``.
    """
    p = (high + low + close) / 3.0
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    return {"P": p, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3}


def zigzag(prices: ArrayLike, threshold_pct: float = 0.05) -> list[dict]:
    """Simple percentage-based zigzag swing pivot detection.

    Returns a list of pivot dicts: ``[{"index": int, "price": float, "type":
    "high"|"low"}, ...]`` in chronological order. ``threshold_pct`` is the
    fractional move required to confirm a new pivot in the opposite
    direction (0.05 = 5%).
    """
    p = _as_array(prices)
    n = len(p)
    if n < 3:
        return []
    pivots: list[dict] = []
    # Seed with first price as tentative low and high
    last_high_idx = 0
    last_low_idx = 0
    direction = 0  # 0 = unknown, 1 = up, -1 = down

    for i in range(1, n):
        if direction >= 0:
            # Looking for new high or trend reversal
            if p[i] > p[last_high_idx]:
                last_high_idx = i
            elif p[i] < p[last_high_idx] * (1 - threshold_pct):
                # Confirm prior high as a pivot
                pivots.append({"index": int(last_high_idx),
                               "price": float(p[last_high_idx]), "type": "high"})
                last_low_idx = i
                direction = -1
        if direction <= 0:
            if p[i] < p[last_low_idx]:
                last_low_idx = i
            elif p[i] > p[last_low_idx] * (1 + threshold_pct):
                pivots.append({"index": int(last_low_idx),
                               "price": float(p[last_low_idx]), "type": "low"})
                last_high_idx = i
                direction = 1
    # Deduplicate adjacent pivots of the same type
    cleaned: list[dict] = []
    for piv in pivots:
        if cleaned and cleaned[-1]["type"] == piv["type"]:
            # Keep the more extreme one
            if (piv["type"] == "high" and piv["price"] > cleaned[-1]["price"]) or \
               (piv["type"] == "low" and piv["price"] < cleaned[-1]["price"]):
                cleaned[-1] = piv
        else:
            cleaned.append(piv)
    return cleaned


# --- Convenience: extract closes/highs/lows/volumes from a bars list -------


def extract_ohlcv(bars: list[dict]):
    """Pull OHLCV columns out of a list of Alpaca-style bar dicts.

    Returns: ``(opens, highs, lows, closes, volumes)`` as numpy arrays.
    Missing keys produce zeros; an empty bars list returns five empty arrays.
    """
    if not bars:
        empty = np.array([], dtype=float)
        return empty, empty, empty, empty, empty
    opens = np.array([float(b.get("o", 0)) for b in bars], dtype=float)
    highs = np.array([float(b.get("h", 0)) for b in bars], dtype=float)
    lows = np.array([float(b.get("l", 0)) for b in bars], dtype=float)
    closes = np.array([float(b.get("c", 0)) for b in bars], dtype=float)
    volumes = np.array([float(b.get("v", 0)) for b in bars], dtype=float)
    return opens, highs, lows, closes, volumes
