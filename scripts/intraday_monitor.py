"""Intraday monitor — the core trading loop.

Two execution modes:

1. **Long-running**: ``python scripts/intraday_monitor.py`` — async loop
   running 9:30–16:00 ET, polling every ``config.POLL_INTERVAL_SECONDS``.
   Use on a persistent host (laptop, VPS).

2. **Single-tick**: ``python scripts/intraday_monitor.py tick`` — one
   iteration and exit. Use this from cron / cloud routines.

Per iteration:
1. Read kill switch — if halted, write heartbeat and skip
2. Read market clock; gate by trading phase (warm-up / active / close-only)
3. Pull positions + account
4. For each watchlist symbol: pull 1m/5m bars, run signal engine, log
5. For BUY composites past consensus: size position, validate, place limit
6. For open positions: stop-loss / take-profit / trailing-stop / time-stop
7. Append one JSON log entry to intraday_log/YYYY-MM-DD.jsonl
8. Update heartbeat
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Any

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

import config           # noqa: E402
import indicators as ind  # noqa: E402
import research         # type: ignore  # noqa: E402
import trade            # type: ignore  # noqa: E402
import signal_engine    # type: ignore  # noqa: E402
import kill_switch      # type: ignore  # noqa: E402
import external_data    # type: ignore  # noqa: E402
from strategies import market_regime  # noqa: E402


# --- Timing helpers --------------------------------------------------------


def _now_et() -> datetime:
    return datetime.now(_ET)


def _today_et_str() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _in_midday_window() -> bool:
    """True during the midday no-entry window (ET). See MIDDAY_NO_ENTRY."""
    try:
        h1, m1 = (int(x) for x in config.MIDDAY_START_ET.split(":"))
        h2, m2 = (int(x) for x in config.MIDDAY_END_ET.split(":"))
    except (AttributeError, ValueError):
        return False
    now = _now_et().time()
    return time(h1, m1) <= now < time(h2, m2)


def _phase() -> str:
    """Classify the current minute into a trading-day phase.

    Returns: ``"pre_open"``, ``"warmup"`` (9:30–10:00), ``"active"``
    (10:00–close-before), ``"close_only"`` (close-before–16:00),
    ``"post_close"``.
    """
    now = _now_et().time()
    open_t = time(9, 30)
    or_end = (datetime.combine(_now_et().date(), open_t) + timedelta(minutes=config.ORB_MINUTES)).time()
    close_only_h, close_only_m = (int(x) for x in config.CLOSE_POSITIONS_BEFORE.split(":"))
    close_only_t = time(close_only_h, close_only_m)
    market_close_t = time(16, 0)

    if now < open_t:
        return "pre_open"
    if now < or_end:
        return "warmup"
    if now < close_only_t:
        return "active"
    if now < market_close_t:
        return "close_only"
    return "post_close"


def _bars_fresh(bars: list) -> bool:
    """True if the latest bar is newer than config.MAX_BAR_STALENESS_MINUTES.

    Guards new entries against feed hiccups. Fails CLOSED (returns False) if a
    timestamp can't be parsed, so we never open a position on unparseable data.
    """
    if not bars:
        return False
    t = bars[-1].get("t", "")
    try:
        if isinstance(t, str):
            ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
        elif isinstance(t, (int, float)):
            ts = datetime.fromtimestamp(t, tz=timezone.utc)
        else:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        return age_min <= config.MAX_BAR_STALENESS_MINUTES
    except (ValueError, TypeError):
        return False


# --- Filesystem ------------------------------------------------------------


def _log_path() -> str:
    d = os.path.join(_ROOT, config.INTRADAY_LOG_DIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_today_et_str()}.jsonl")


def _append_log(entry: dict[str, Any]) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        print(f"[intraday_monitor] log write failed: {exc}", file=sys.stderr)


def _load_watchlist() -> dict[str, Any]:
    try:
        with open(os.path.join(_ROOT, config.WATCHLIST_PATH), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[intraday_monitor] watchlist load failed: {exc}", file=sys.stderr)
        return {"watchlist": []}


def _load_state() -> dict[str, Any]:
    """State file at ``intraday_log/state-<date>.json`` — tracks per-symbol
    open-entry timestamps, trailing stops, daily trade count, etc."""
    path = os.path.join(_ROOT, config.INTRADAY_LOG_DIR, f"state-{_today_et_str()}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"trades_today": 0, "positions_meta": {}, "start_equity": None}


def _save_state(state: dict[str, Any]) -> None:
    os.makedirs(os.path.join(_ROOT, config.INTRADAY_LOG_DIR), exist_ok=True)
    path = os.path.join(_ROOT, config.INTRADAY_LOG_DIR, f"state-{_today_et_str()}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        print(f"[intraday_monitor] state write failed: {exc}", file=sys.stderr)


def _update_heartbeat(status: str, extra: dict | None = None) -> None:
    path = os.path.join(_ROOT, config.HEARTBEAT_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        hb = {}
    hb["last_run"] = datetime.now(timezone.utc).isoformat()
    hb["last_routine"] = "Intraday Monitor"
    hb["status"] = status
    if extra:
        hb.update(extra)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(hb, fh, indent=2)
    except OSError as exc:
        print(f"[intraday_monitor] heartbeat write failed: {exc}", file=sys.stderr)


# --- Pattern detection (lightweight, only adjusts composite confidence) ----


def detect_chart_patterns(bars_5m: list, bars_1d: list) -> list[dict[str, Any]]:
    """Real-time pattern detection. Used as a confidence modifier, never a
    standalone signal. Returns a list of detected pattern dicts.
    """
    out: list[dict[str, Any]] = []
    if not bars_5m or len(bars_5m) < 20:
        return out
    opens, highs, lows, closes, vols = ind.extract_ohlcv(bars_5m)

    # Candlestick patterns on the last 1–3 5m bars
    out.extend(_candlestick_patterns(opens, highs, lows, closes))
    # Multi-bar patterns
    out.extend(_multibar_patterns(opens, highs, lows, closes, vols))
    return out


def _candlestick_patterns(opens, highs, lows, closes) -> list[dict[str, Any]]:
    n = len(closes)
    if n < 3:
        return []
    out = []
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    o_prev, c_prev = opens[-2], closes[-2]
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return out
    upper = h - max(o, c)
    lower = min(o, c) - l

    if lower > 2 * body and upper < body and c > o and (body / rng) < 0.35:
        out.append({"pattern": "hammer", "direction": "bullish",
                    "confidence": float(min(lower / rng, 1.0)),
                    "target_price": float(c + 2 * rng),
                    "invalidation_price": float(l)})
    if upper > 2 * body and lower < body and c < o and (body / rng) < 0.35:
        out.append({"pattern": "shooting_star", "direction": "bearish",
                    "confidence": float(min(upper / rng, 1.0)),
                    "target_price": float(c - 2 * rng),
                    "invalidation_price": float(h)})
    if c_prev < o_prev and c > o and o < c_prev and c > o_prev:
        out.append({"pattern": "bullish_engulfing", "direction": "bullish",
                    "confidence": 0.7, "target_price": float(c + 2 * rng),
                    "invalidation_price": float(l)})
    if c_prev > o_prev and c < o and o > c_prev and c < o_prev:
        out.append({"pattern": "bearish_engulfing", "direction": "bearish",
                    "confidence": 0.7, "target_price": float(c - 2 * rng),
                    "invalidation_price": float(h)})
    # Doji (indecision) — emit as neutral, reduces composite confidence
    if body / rng < 0.1:
        out.append({"pattern": "doji", "direction": "neutral", "confidence": 0.5,
                    "target_price": float(c), "invalidation_price": float(c)})
    return out


def _multibar_patterns(opens, highs, lows, closes, vols) -> list[dict[str, Any]]:
    """Detect bull-flag / bear-flag / ascending/descending triangle / double top/bottom."""
    n = len(closes)
    if n < 10:
        return []
    out = []
    window = 10
    recent_high = float(np.max(highs[-window:]))
    recent_low = float(np.min(lows[-window:]))
    rng = recent_high - recent_low
    last = float(closes[-1])

    # Bull flag — strong up-move (last 20 bars rose) followed by tight 10-bar consolidation
    if n >= 30:
        impulse = float(closes[-window]) - float(closes[-30])
        impulse_pct = impulse / max(float(closes[-30]), 1e-6)
        consolidation_range = rng / max(last, 1e-6)
        if impulse_pct > 0.02 and 0 < consolidation_range < 0.015:
            out.append({"pattern": "bull_flag", "direction": "bullish",
                        "confidence": 0.7,
                        "target_price": float(last + impulse),
                        "invalidation_price": recent_low})
        elif impulse_pct < -0.02 and 0 < consolidation_range < 0.015:
            out.append({"pattern": "bear_flag", "direction": "bearish",
                        "confidence": 0.7,
                        "target_price": float(last + impulse),
                        "invalidation_price": recent_high})

    # Double top / double bottom — two highs/lows within 0.5% of each other
    highs_window = highs[-window:]
    lows_window = lows[-window:]
    top1 = float(np.max(highs_window))
    top2 = float(np.max(np.delete(highs_window, np.argmax(highs_window))))
    bot1 = float(np.min(lows_window))
    bot2 = float(np.min(np.delete(lows_window, np.argmin(lows_window))))
    if abs(top1 - top2) / top1 < 0.005 and last < (top1 + top2) / 2 * 0.995:
        out.append({"pattern": "double_top", "direction": "bearish", "confidence": 0.6,
                    "target_price": float(recent_low), "invalidation_price": float(top1)})
    if abs(bot1 - bot2) / max(bot1, 1e-6) < 0.005 and last > (bot1 + bot2) / 2 * 1.005:
        out.append({"pattern": "double_bottom", "direction": "bullish", "confidence": 0.6,
                    "target_price": float(recent_high), "invalidation_price": float(bot1)})

    # Ascending triangle (flat resistance, rising lows) — fit line on last 10 lows
    if n >= window:
        x = np.arange(window)
        slope_lo = float(np.polyfit(x, lows[-window:], 1)[0])
        slope_hi = float(np.polyfit(x, highs[-window:], 1)[0])
        avg_high = float(np.mean(highs[-window:]))
        if slope_lo > 0 and abs(slope_hi) < avg_high * 0.0005:
            out.append({"pattern": "ascending_triangle", "direction": "bullish", "confidence": 0.6,
                        "target_price": float(recent_high + rng),
                        "invalidation_price": recent_low})
        if slope_hi < 0 and abs(slope_lo) < avg_high * 0.0005:
            out.append({"pattern": "descending_triangle", "direction": "bearish", "confidence": 0.6,
                        "target_price": float(recent_low - rng),
                        "invalidation_price": recent_high})

    return out


def _patterns_to_boost(patterns: list[dict]) -> float:
    """Sum the directional pull of detected patterns (clamped to PATTERN_BOOST_MAX)."""
    if not patterns:
        return 0.0
    boost = 0.0
    for p in patterns:
        sign = {"bullish": +1.0, "bearish": -1.0}.get(p.get("direction", ""), 0.0)
        boost += sign * float(p.get("confidence", 0.0)) * 0.1
    return float(max(-config.PATTERN_BOOST_MAX, min(config.PATTERN_BOOST_MAX, boost)))


# --- Position management --------------------------------------------------


def monitor_position(symbol: str, position: dict, current_bar: dict,
                     atr_value: float, meta: dict) -> str:
    """Decide whether to keep, stop, take-profit, or time-out a position.

    Returns one of ``"HOLD"``, ``"CLOSE_STOP"``, ``"CLOSE_PROFIT"``,
    ``"CLOSE_TIME"``. ``meta`` is the per-symbol state dict and is mutated in
    place (trailing stop, peak price).
    """
    # Use the broker's authoritative mark for stop/target decisions; the bar
    # close is only a fallback. Bars can lag or be stale on the free feed, and
    # acting on a phantom price can fire a false stop on a winning position.
    current_price = float(position.get("current_price") or current_bar.get("c", 0))
    qty = float(position.get("qty", 0))      # signed: + long, - short
    if qty == 0 or current_price <= 0:
        return "HOLD"
    avg_entry = float(position.get("avg_entry_price", current_price))
    is_long = qty > 0
    take_profit = float(meta.get("take_profit") or 0.0)

    # R = initial risk per share (entry → initial stop distance). Persisted on
    # first sight so a later breakeven move doesn't shrink it.
    if meta.get("initial_risk") is None:
        if is_long:
            init_stop = float(meta.get("stop_loss") or avg_entry * (1 - config.STOP_LOSS_PCT))
            meta["initial_risk"] = max(avg_entry - init_stop, avg_entry * 0.001)
        else:
            init_stop = float(meta.get("stop_loss") or avg_entry * (1 + config.STOP_LOSS_PCT))
            meta["initial_risk"] = max(init_stop - avg_entry, avg_entry * 0.001)
    r_unit = float(meta["initial_risk"])

    if is_long:
        hard_stop = float(meta.get("stop_loss") or avg_entry * (1 - config.STOP_LOSS_PCT))
        peak = max(float(meta.get("peak_price") or current_price), current_price)
        meta["peak_price"] = peak
        favorable_dist = current_price - avg_entry
        # At +1R the trade has earned protection: stop moves to breakeven.
        if favorable_dist >= config.BREAKEVEN_AT_R * r_unit and hard_stop < avg_entry:
            hard_stop = avg_entry
            meta["stop_loss"] = avg_entry
        # Trailing stop arms only after the trade is meaningfully in profit —
        # trailing from bar one just donates the position to noise.
        if (atr_value and atr_value > 0
                and peak - avg_entry >= config.TRAIL_ACTIVATION_R * r_unit):
            trail = peak - config.TRAILING_STOP_ATR_MULT * atr_value
            meta["trailing_stop"] = max(float(meta.get("trailing_stop") or 0.0), trail)
        effective_stop = max(hard_stop, float(meta.get("trailing_stop") or 0.0))
        if current_price <= effective_stop:
            return "CLOSE_STOP"
        if take_profit and current_price >= take_profit:
            return "CLOSE_PROFIT"
        favorable_move = favorable_dist / avg_entry
    else:
        # Short: stop is ABOVE entry, take-profit BELOW, trailing stop trails
        # the trough downward; only ever moves down.
        hard_stop = float(meta.get("stop_loss") or avg_entry * (1 + config.STOP_LOSS_PCT))
        trough = min(float(meta.get("trough_price") or current_price), current_price)
        meta["trough_price"] = trough
        favorable_dist = avg_entry - current_price
        if favorable_dist >= config.BREAKEVEN_AT_R * r_unit and hard_stop > avg_entry:
            hard_stop = avg_entry
            meta["stop_loss"] = avg_entry
        if (atr_value and atr_value > 0
                and avg_entry - trough >= config.TRAIL_ACTIVATION_R * r_unit):
            trail = trough + config.TRAILING_STOP_ATR_MULT * atr_value
            prev = meta.get("trailing_stop")
            meta["trailing_stop"] = trail if prev is None else min(float(prev), trail)
        ts = meta.get("trailing_stop")
        effective_stop = min(hard_stop, float(ts)) if ts is not None else hard_stop
        if current_price >= effective_stop:
            return "CLOSE_STOP"
        if take_profit and current_price <= take_profit:
            return "CLOSE_PROFIT"
        favorable_move = favorable_dist / avg_entry

    # Time stop — entered too long ago without favorable movement (either side)
    entry_time = meta.get("entry_time")
    if entry_time:
        try:
            t0 = datetime.fromisoformat(entry_time)
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            held_minutes = (datetime.now(timezone.utc) - t0).total_seconds() / 60.0
            if held_minutes >= config.TIME_STOP_MINUTES and favorable_move < config.TIME_STOP_MIN_MOVE_PCT:
                return "CLOSE_TIME"
        except (ValueError, TypeError):
            pass
    return "HOLD"


# --- The main iteration ---------------------------------------------------


def run_iteration() -> dict[str, Any]:
    """One full poll cycle. Returns a structured log entry (also persisted)."""
    iter_ts = datetime.now(timezone.utc).isoformat()
    phase = _phase()

    # Kill-switch check before anything else
    if kill_switch.is_halted():
        entry = {"timestamp": iter_ts, "phase": phase, "halted": True,
                 "signals": [], "open_positions": []}
        _append_log(entry)
        _update_heartbeat("halted")
        return entry

    watchlist = _load_watchlist()
    symbols = [e["symbol"] for e in watchlist.get("watchlist", [])]
    has_crypto = any(research.is_crypto(s) for s in symbols)
    equities_open = phase in ("warmup", "active", "close_only")

    # Skip only when equities are closed AND there is no 24/7 crypto to trade.
    if not equities_open and not has_crypto:
        entry = {"timestamp": iter_ts, "phase": phase, "skipped": "market_closed",
                 "signals": [], "open_positions": []}
        _append_log(entry)
        _update_heartbeat("idle")
        return entry

    state = _load_state()
    account = research.get_account()
    if state.get("start_equity") is None:
        state["start_equity"] = float(account.get("equity", 0.0))
    positions_list = research.get_positions()
    # Key by normalized symbol so "BTC/USD" (orders) matches "BTCUSD" (positions).
    positions_by_sym = {p["symbol"].upper().replace("/", ""): p for p in positions_list}

    # Prune metas whose close order has filled (position gone). Only metas
    # with a submitted close are pruned — pending-entry metas must survive
    # until their entry order fills and the position appears.
    for _k in list(state.get("positions_meta", {}).keys()):
        _m = state["positions_meta"].get(_k) or {}
        if _m.get("close_order_id") and _k not in positions_by_sym:
            state["positions_meta"].pop(_k, None)

    # Daily-loss kill switch
    ks_status = kill_switch.check_daily_loss(state["start_equity"], float(account.get("equity", 0.0)))
    if ks_status.get("halted"):
        kill_switch.emergency_close_all(reason=ks_status["reason"])
        entry = {"timestamp": iter_ts, "phase": phase, "halted": True,
                 "kill_switch": ks_status, "signals": [], "open_positions": positions_list}
        _append_log(entry)
        _update_heartbeat("halted")
        _save_state(state)
        return entry

    # Prefetch daily bars once for all watchlist + held symbols (reused for
    # regime, per-symbol analysis, and the correlation-cluster check).
    held_syms = [p["symbol"].upper() for p in positions_list]
    daily_bars_by_symbol: dict[str, list] = {}
    for sym in dict.fromkeys([s.upper() for s in symbols] + held_syms + ["SPY"]):
        daily_bars_by_symbol[sym] = research.get_bars(sym, timeframe="1Day", limit=120)

    # Regime classification (once per iteration)
    regime = market_regime.classify(daily_bars_by_symbol.get("SPY", []))

    signals_out: list[dict[str, Any]] = []
    placed_orders: list[dict[str, Any]] = []

    for symbol in symbols:
        sym_crypto = research.is_crypto(symbol)
        sym_key = symbol.upper().replace("/", "")
        # Equity symbols only trade during market hours; crypto trades 24/7.
        if not sym_crypto and not equities_open:
            continue
        bars_1m = research.get_bars(symbol, timeframe="1Min", limit=200)
        bars_5m = research.get_bars(symbol, timeframe="5Min", limit=200)
        bars_1d = daily_bars_by_symbol.get(symbol.upper()) or research.get_bars(symbol, timeframe="1Day", limit=120)
        if not bars_1m and not bars_5m:
            continue

        # Data-freshness guard: skip NEW entries if the latest intraday bar is
        # stale (defense against feed hiccups). Position management still runs
        # off the broker mark, so existing stops are unaffected.
        data_fresh = _bars_fresh(bars_1m or bars_5m)

        # ATR for sizing & trailing stops
        _, h5, l5, c5, _ = ind.extract_ohlcv(bars_5m or [])
        atr_arr = ind.atr(h5, l5, c5, period=config.ATR_PERIOD) if len(c5) > config.ATR_PERIOD else None
        atr_value = float(atr_arr[-1]) if atr_arr is not None and len(atr_arr) and not np.isnan(atr_arr[-1]) else None

        # Run strategies + patterns + sentiment, then aggregate.
        # Sentiment (Alpha Vantage) and pattern boost stack into composite
        # confidence — capped via config to prevent runaway.
        strategy_signals = signal_engine.run_all_strategies(
            symbol, bars_1m, bars_5m, bars_1d, account, positions_list)
        patterns = detect_chart_patterns(bars_5m, bars_1d)
        sentiment_score = external_data.news_sentiment(symbol)
        sentiment_boost = float(max(-config.SENTIMENT_BOOST_MAX,
                                    min(config.SENTIMENT_BOOST_MAX,
                                        sentiment_score * config.SENTIMENT_BOOST_MAX)))
        total_boost = _patterns_to_boost(patterns) + sentiment_boost
        composite = signal_engine.aggregate_signals(
            strategy_signals, regime["regime"], regime["weights"], total_boost)

        action_taken = "NONE"
        order_info: dict[str, Any] | None = None
        earnings_blocker = False

        # ENTRY — long on BUY, short on SELL. Equities only in the active phase;
        # crypto any time the monitor runs. Flat in this symbol, under the daily
        # cap, and on fresh data.
        can_enter_phase = sym_crypto or phase == "active"
        if (can_enter_phase
                and composite["signal"] in ("BUY", "SELL")
                and state.get("trades_today", 0) < config.MAX_DAILY_TRADES
                and sym_key not in positions_by_sym):
            # Defensive: never open a new position into a binary earnings event.
            # has_upcoming_earnings is a no-op for crypto.
            earnings_blocker = external_data.has_upcoming_earnings(
                symbol, hours=config.EARNINGS_BLACKOUT_HOURS)
            if not data_fresh:
                action_taken = "SKIPPED:stale_data"
            elif earnings_blocker:
                action_taken = f"SKIPPED:earnings_within_{config.EARNINGS_BLACKOUT_HOURS}h"
            elif not sym_crypto and config.MIDDAY_NO_ENTRY and _in_midday_window():
                # Replay-validated (exit_replay 2026-07-02): midday equity
                # entries were -0.078%/trade in BOTH regime halves; skipping
                # them lifted replay cum P&L from +1.43% to +3.69%. Composite
                # confidence showed no predictive power in this window.
                action_taken = "SKIPPED:midday_window"
            else:
                side = "buy" if composite["signal"] == "BUY" else "sell"
                sizing = signal_engine.calculate_position_size(
                    composite, account, regime["regime"], atr_value, symbol, watchlist)
                qty = sizing["qty"]
                entry_price = composite.get("entry_price") or (float(c5[-1]) if len(c5) else 0.0)
                if qty > 0 and entry_price > 0:
                    limit_price = round(float(entry_price), 2)
                    pos_snapshot = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                                     "market_value": float(p["market_value"])} for p in positions_list]
                    # Correlation-cluster gate (fail-open if data missing)
                    corr_ok, corr_reason = signal_engine.check_correlation_exposure(
                        symbol, sizing["notional"], pos_snapshot,
                        float(account.get("equity", 0.0)), daily_bars_by_symbol)
                    if not corr_ok:
                        action_taken = f"REJECTED:{corr_reason}"
                    else:
                        ok, why = trade.validate_order(
                            symbol, qty, side, limit_price,
                            float(account.get("equity", 0.0)), pos_snapshot, watchlist)
                        if ok:
                            try:
                                order = trade.place_order(symbol, qty, side, limit_price)
                                order_info = {"order": order, "sizing": sizing}
                                action_taken = "SHORT_OPENED" if side == "sell" else "ORDER_PLACED"
                                placed_orders.append(order)
                                state["trades_today"] = int(state.get("trades_today", 0)) + 1
                                state.setdefault("positions_meta", {})[sym_key] = {
                                    "direction": "long" if side == "buy" else "short",
                                    "entry_time": datetime.now(timezone.utc).isoformat(),
                                    "entry_price": entry_price,
                                    "stop_loss": composite.get("stop_loss"),
                                    "take_profit": composite.get("take_profit"),
                                    "peak_price": entry_price,
                                    "trough_price": entry_price,
                                    "trailing_stop": None,
                                    "opening_strategies": composite.get("opening_strategies", []),
                                }
                            except Exception as exc:  # noqa: BLE001
                                action_taken = f"ORDER_FAILED:{exc}"
                        else:
                            action_taken = f"REJECTED:{why}"

        # MANAGE existing position — long or short
        if sym_key in positions_by_sym:
            position = positions_by_sym[sym_key]
            meta = state.setdefault("positions_meta", {}).setdefault(sym_key, {})
            last_bar = (bars_1m or bars_5m or [{"c": position.get("current_price", 0)}])[-1]
            # In the close-only window, force-flatten EQUITIES (no overnight
            # exposure) unless flagged swing. Crypto trades 24/7 — never EOD-flattened.
            if (phase == "close_only" and not config.OVERNIGHT_ALLOWED
                    and not sym_crypto and not meta.get("swing", False)):
                decision = "CLOSE_EOD"
            else:
                decision = monitor_position(symbol, position, last_bar, atr_value or 0.0, meta)
            if decision in ("CLOSE_STOP", "CLOSE_PROFIT", "CLOSE_TIME", "CLOSE_EOD"):
                current_price = float(position.get("current_price") or last_bar.get("c", 0))
                qty_held = float(position.get("qty", 0))     # signed
                # Skip dust positions: tiny leftover crypto fractions that can't
                # be meaningfully closed and would just spam errors each tick.
                if sym_crypto and abs(qty_held) < 1e-6:
                    action_taken = "DUST_HELD"
                    state.get("positions_meta", {}).pop(sym_key, None)
                elif qty_held != 0 and current_price > 0:
                    # Pending-close guard: a limit close that hasn't filled yet
                    # must not trigger a second close (Alpaca rejects it as a
                    # wash trade / insufficient qty — seen live on MARA 6/11).
                    pending_id = meta.get("close_order_id")
                    pending_at = meta.get("close_submitted_at")
                    proceed = True
                    if pending_id and pending_at:
                        try:
                            t0 = datetime.fromisoformat(pending_at)
                            age_min = (datetime.now(timezone.utc) - t0).total_seconds() / 60.0
                        except (ValueError, TypeError):
                            age_min = None
                        if age_min is not None and age_min < config.CLOSE_RETRY_MINUTES:
                            action_taken = "CLOSE_PENDING"
                            proceed = False
                        else:
                            # Stale unfilled close — cancel it and re-place at a
                            # fresh price so the position doesn't hang open.
                            try:
                                trade.cancel_order(pending_id)
                            except Exception as exc:  # noqa: BLE001
                                print(f"[intraday_monitor] cancel stale close failed: {exc}",
                                      file=sys.stderr)
                    if proceed:
                        if qty_held > 0:   # close a long → SELL
                            close_side, close_qty = "sell", qty_held
                            limit_price = round(current_price * 0.998, 2)
                        else:              # cover a short → BUY
                            close_side, close_qty = "buy", abs(qty_held)
                            limit_price = round(current_price * 1.002, 2)
                        try:
                            order = trade.place_order(symbol, close_qty, close_side, limit_price)
                            action_taken = decision
                            order_info = {"order": order, "exit_reason": decision}
                            placed_orders.append(order)
                            meta["close_order_id"] = (order or {}).get("id")
                            meta["close_submitted_at"] = datetime.now(timezone.utc).isoformat()
                            # Accuracy close-loop: realised P&L → credit/charge each
                            # strategy that voted to open this position. Guarded so
                            # a re-placed close can't double-record the outcome.
                            if not meta.get("close_recorded"):
                                avg_entry = float(position.get("avg_entry_price", current_price))
                                if qty_held > 0:
                                    pnl_pct = (current_price - avg_entry) / avg_entry if avg_entry else 0.0
                                else:
                                    pnl_pct = (avg_entry - current_price) / avg_entry if avg_entry else 0.0
                                outcome = "WIN" if pnl_pct > 0 else "LOSS"
                                direction = "BUY" if qty_held > 0 else "SELL"
                                for strat in meta.get("opening_strategies", []):
                                    try:
                                        signal_engine.update_accuracy_tracker(
                                            symbol, strat, direction, outcome, pnl_pct)
                                    except Exception as exc:  # noqa: BLE001
                                        print(f"[intraday_monitor] accuracy update failed: {exc}",
                                              file=sys.stderr)
                                meta["close_recorded"] = True
                            # Meta stays until the position is confirmed flat —
                            # pruned at the top of the next iteration.
                        except Exception as exc:  # noqa: BLE001
                            action_taken = f"CLOSE_FAILED:{exc}"

        signals_out.append({
            "symbol": symbol,
            "strategy_signals": [
                {"strategy": s["strategy"], "signal": s["signal"],
                 "confidence": s["confidence"], "reason": s["reason"]}
                for s in strategy_signals
            ],
            "composite_signal": composite["signal"],
            "composite_confidence": composite["confidence"],
            "composite_reason": composite["reason"],
            "patterns_detected": [p["pattern"] for p in patterns],
            "sentiment_score": sentiment_score,
            "sentiment_boost": sentiment_boost,
            "earnings_blocker": earnings_blocker,
            "action_taken": action_taken,
            "order_id": (order_info.get("order", {}) or {}).get("id") if order_info else None,
        })

    _save_state(state)
    entry = {
        "timestamp": iter_ts,
        "phase": phase,
        "regime": regime["regime"],
        "regime_details": regime.get("details", ""),
        "symbols_scanned": symbols,
        "signals": signals_out,
        "open_positions": positions_list,
        "portfolio_value": float(account.get("equity", 0.0)),
        "daily_pnl": float(account.get("equity", 0.0)) - float(state["start_equity"] or 0.0),
        "trades_today": state.get("trades_today", 0),
        "orders_placed_this_iteration": placed_orders,
    }
    _append_log(entry)
    _update_heartbeat("active", {"trades_today": state.get("trades_today", 0),
                                  "portfolio_value": entry["portfolio_value"],
                                  "daily_pnl": entry["daily_pnl"]})
    return entry


# --- Long-running loop ----------------------------------------------------


async def run_market_day() -> None:
    """Long-running async loop. Stops automatically after market close."""
    print(f"[intraday_monitor] starting market-day loop at {_now_et().isoformat()}")
    while True:
        try:
            entry = run_iteration()
            phase = entry.get("phase", "unknown")
            if phase == "post_close":
                print(f"[intraday_monitor] market closed; exiting loop.")
                _update_heartbeat("ok", {"last_routine": "Intraday Monitor (closed)"})
                break
        except Exception as exc:  # noqa: BLE001 — loop must not die
            print(f"[intraday_monitor] iteration error: {exc}", file=sys.stderr)
            _update_heartbeat("error")
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


# --- CLI -------------------------------------------------------------------


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if mode == "tick":
        result = run_iteration()
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("orders_placed_this_iteration",)}, indent=2,
                         default=str))
    elif mode == "loop":
        asyncio.run(run_market_day())
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        sys.exit(2)
