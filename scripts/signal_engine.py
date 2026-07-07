"""Signal aggregator.

Runs every active strategy on a symbol, applies regime + accuracy-based
weights, and emits a composite signal only when the weighted consensus
exceeds ``config.CONSENSUS_THRESHOLD``. Position sizing here.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import indicators as ind  # noqa: E402
import config             # noqa: E402
from strategies import STRATEGIES, market_regime  # noqa: E402


def run_all_strategies(symbol: str, bars_1m: list, bars_5m: list, bars_1d: list,
                       account: dict, positions: list) -> list[dict[str, Any]]:
    """Run every active strategy on a symbol and return their raw signals.

    A strategy that raises is logged to stderr and skipped — never aborts the
    others.
    """
    out: list[dict[str, Any]] = []
    for mod in STRATEGIES:
        try:
            sig = mod.analyze(symbol, bars_1m, bars_5m, bars_1d, account, positions)
            if isinstance(sig, dict) and "signal" in sig:
                out.append(sig)
            else:
                print(f"[signal_engine] {getattr(mod,'NAME','?')} returned malformed signal",
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — keep other strategies running
            print(f"[signal_engine] strategy {getattr(mod,'NAME','?')} failed: {exc}",
                  file=sys.stderr)
    return out


def aggregate_signals(signals: list[dict[str, Any]], regime: str,
                      regime_weights: dict[str, float] | None = None,
                      pattern_boost: float = 0.0) -> dict[str, Any]:
    """Weighted vote across strategy signals.

    ``regime_weights`` overlays on top of ``config.STRATEGY_WEIGHTS``.
    ``pattern_boost`` is in [-PATTERN_BOOST_MAX, +PATTERN_BOOST_MAX] and
    nudges composite confidence in the direction it suggests.

    Returns a composite signal with the same shape as a strategy output, plus
    a ``components`` list of the contributing strategies.
    """
    if not signals:
        return _hold("no_strategy_signals_received")

    accuracy = _load_accuracy_tracker()
    base = config.STRATEGY_WEIGHTS
    regime_w = regime_weights or {}

    weights_buy = 0.0
    weights_sell = 0.0
    weights_hold = 0.0
    total_weight = 0.0
    components: list[dict[str, Any]] = []

    # Aggregate stop/take levels per direction (weighted by per-voter weight)
    buy_entry, buy_stop, buy_tp, buy_w = 0.0, 0.0, 0.0, 0.0
    sell_entry, sell_stop, sell_tp, sell_w = 0.0, 0.0, 0.0, 0.0

    for sig in signals:
        name = sig.get("strategy", "?")
        base_w = base.get(name, 1.0)
        regime_mult = regime_w.get(name, 1.0)
        acc_mult = _accuracy_multiplier(accuracy.get(name, {}))
        conf = float(sig.get("confidence", 0.0))
        # Combined weight for this vote
        w = base_w * regime_mult * acc_mult * max(conf, 0.05)
        total_weight += w
        if sig["signal"] == "BUY":
            weights_buy += w
            buy_entry += (sig.get("entry_price") or 0.0) * w
            buy_stop += (sig.get("stop_loss") or 0.0) * w
            buy_tp += (sig.get("take_profit") or 0.0) * w
            buy_w += w
        elif sig["signal"] == "SELL":
            weights_sell += w
            sell_entry += (sig.get("entry_price") or 0.0) * w
            sell_stop += (sig.get("stop_loss") or 0.0) * w
            sell_tp += (sig.get("take_profit") or 0.0) * w
            sell_w += w
        else:
            weights_hold += w

        components.append({
            "strategy": name,
            "signal": sig["signal"],
            "confidence": conf,
            "weight": w,
            "reason": sig.get("reason", ""),
            "entry_price": sig.get("entry_price"),
            "stop_loss": sig.get("stop_loss"),
            "take_profit": sig.get("take_profit"),
        })

    if total_weight <= 0:
        return _hold("zero_total_weight")

    buy_share = weights_buy / total_weight
    sell_share = weights_sell / total_weight

    # In TRENDING_DOWN regime, only allow SELLs (cash preservation)
    if regime == "TRENDING_DOWN" and buy_share > sell_share:
        composite_signal = "HOLD"
        composite_conf = 0.0
        reason = "TRENDING_DOWN regime suppresses BUY signals"
        return _compose(composite_signal, composite_conf, reason, components, None, None, None)

    if buy_share >= max(config.CONSENSUS_THRESHOLD, sell_share):
        composite_signal = "BUY"
        side_mult = _side_multiplier(accuracy, "BUY")
        composite_conf = min(1.0, (buy_share + pattern_boost) * side_mult)
        if buy_w > 0:
            entry_price = buy_entry / buy_w
            stop = buy_stop / buy_w if buy_stop > 0 else None
            tp = buy_tp / buy_w if buy_tp > 0 else None
        else:
            entry_price, stop, tp = None, None, None
        reason = (f"BUY consensus {buy_share:.2%}; pattern_boost {pattern_boost:+.3f}; "
                  f"side_mult {side_mult:.2f}")
        owners = [c["strategy"] for c in components if c["signal"] == "BUY" and c["weight"] > 0]
        return _compose(composite_signal, composite_conf, reason, components,
                        entry_price, stop, tp, owners)

    if sell_share >= max(config.CONSENSUS_THRESHOLD, buy_share):
        composite_signal = "SELL"
        side_mult = _side_multiplier(accuracy, "SELL")
        composite_conf = min(1.0, (sell_share + abs(min(pattern_boost, 0.0))) * side_mult)
        if sell_w > 0:
            entry_price = sell_entry / sell_w
            stop = sell_stop / sell_w if sell_stop > 0 else None
            tp = sell_tp / sell_w if sell_tp > 0 else None
        else:
            entry_price, stop, tp = None, None, None
        reason = (f"SELL consensus {sell_share:.2%}; pattern_boost {pattern_boost:+.3f}; "
                  f"side_mult {side_mult:.2f}")
        owners = [c["strategy"] for c in components if c["signal"] == "SELL" and c["weight"] > 0]
        return _compose(composite_signal, composite_conf, reason, components,
                        entry_price, stop, tp, owners)

    return _compose("HOLD", 0.0,
                    f"no consensus (BUY {buy_share:.2%}, SELL {sell_share:.2%})",
                    components, None, None, None, [])


def calculate_position_size(signal: dict[str, Any], account: dict,
                            regime: str, atr_value: float | None,
                            symbol: str, watchlist: dict | None = None) -> dict[str, Any]:
    """Kelly-ish position sizing under the policy.

    Returns ``{"qty": float, "notional": float, "pct_of_equity": float, "reason": str}``.
    ``qty`` rounds DOWN to whole shares.
    """
    equity = float(account.get("equity", 0.0) or 0.0)
    cash = float(account.get("cash", 0.0) or 0.0)
    if equity <= 0:
        return {"qty": 0, "notional": 0.0, "pct_of_equity": 0.0,
                "reason": "no equity available"}

    confidence = float(signal.get("confidence", 0.0))
    price = float(signal.get("entry_price") or 0.0)
    if price <= 0:
        return {"qty": 0, "notional": 0.0, "pct_of_equity": 0.0,
                "reason": "no entry price on signal"}

    # Base sizing
    pct = config.BASE_POSITION_PCT
    if confidence >= 0.80 and regime in ("TRENDING_UP", "TRENDING_DOWN"):
        pct = config.MAX_POSITION_PCT
    if regime == "HIGH_VOLATILITY":
        pct = config.MIN_POSITION_PCT

    # Fixed-fractional risk sizing: when the signal has a stop, target a
    # constant equity risk at that stop instead of constant notional. The
    # notional pct caps below still bound the result — risk sizing decides
    # the target WITHIN the caps, never above them.
    risk_notional = None
    stop = float(signal.get("stop_loss") or 0.0)
    if getattr(config, "RISK_SIZING_ENABLED", False) and stop > 0 and price > 0:
        stop_dist = abs(price - stop)
        if stop_dist > price * 0.0005:      # ignore degenerate paper-thin stops
            risk_pct = config.RISK_PER_TRADE_PCT
            if confidence >= 0.80 and regime in ("TRENDING_UP", "TRENDING_DOWN"):
                risk_pct *= 1.5             # conviction bumps the risk budget
            if regime == "HIGH_VOLATILITY":
                risk_pct *= 0.6
            risk_pct = min(risk_pct, getattr(config, "RISK_PER_TRADE_MAX_PCT", 0.004))
            risk_notional = (risk_pct * equity) / (stop_dist / price)

    # Clamp to the SAME effective cap validate_order enforces:
    # min(global max_single_position_pct, per-symbol max_allocation_pct).
    # Sizing above the cap and letting the validator reject it silently
    # discards otherwise-valid entries (seen live: AAPL 6/3, sized 7.76%
    # against a 5% cap and rejected instead of resized).
    if watchlist:
        entries = {e["symbol"].upper(): e for e in watchlist.get("watchlist", [])}
        global_cap = float(watchlist.get("max_single_position_pct", 5))
        sym_cap = float(entries.get(symbol.upper(), {}).get(
            "max_allocation_pct", config.MAX_POSITION_PCT * 100))
        pct = min(pct, global_cap / 100.0, sym_cap / 100.0)

    # ATR-based volatility scaling: tighter risk in high ATR → smaller size
    if atr_value and atr_value > 0:
        atr_pct = atr_value / price
        if atr_pct > 0.05:        # >5% ATR — exceptionally volatile
            pct *= 0.5

    target_notional = pct * equity
    if risk_notional is not None:
        # Risk target, bounded by the notional cap: min() guarantees risk
        # sizing can never exceed what the caps would have allowed.
        target_notional = min(risk_notional, target_notional)

    # Cash reserve enforcement
    min_cash = config.CASH_RESERVE_PCT * equity
    max_notional_cash = max(0.0, cash - min_cash)
    if target_notional > max_notional_cash:
        target_notional = max_notional_cash

    crypto = "/" in (symbol or "")
    if crypto and getattr(config, "ALLOW_FRACTIONAL", True):
        qty = round(target_notional / price, 6)          # fractional units
    else:
        qty = math.floor(target_notional / price)        # whole shares
    notional = qty * price
    mode = "risk-based" if risk_notional is not None else "notional"
    return {
        "qty": float(qty) if crypto else int(qty),
        "notional": float(notional),
        "pct_of_equity": float(notional / equity) if equity else 0.0,
        "reason": (f"{mode} size (conf {confidence:.2f}, regime {regime}, cap {pct*100:.2f}%) "
                   f"→ {qty} units × ${price:.2f} = ${notional:,.2f}"),
    }


def update_accuracy_tracker(symbol: str, strategy: str, signal: str,
                            outcome: str, pnl_pct: float = 0.0) -> dict[str, Any]:
    """Record a trade outcome for a strategy.

    ``outcome`` should be ``"WIN"`` or ``"LOSS"``. ``pnl_pct`` is the realised
    return on the position (e.g. 0.018 = +1.8%). Persisted to
    ``models/accuracy_tracker.json``.
    """
    path = os.path.join(_ROOT, config.ACCURACY_TRACKER_PATH)
    tracker = _load_accuracy_tracker()
    record = tracker.get(strategy, {"wins": 0, "losses": 0, "pnl_sum": 0.0,
                                    "trades": [], "updated_at": None})
    if outcome == "WIN":
        record["wins"] = int(record.get("wins", 0)) + 1
    elif outcome == "LOSS":
        record["losses"] = int(record.get("losses", 0)) + 1
    record["pnl_sum"] = float(record.get("pnl_sum", 0.0)) + float(pnl_pct)
    record["trades"] = (record.get("trades", []) + [{
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "signal": signal,
        "outcome": outcome,
        "pnl_pct": float(pnl_pct),
    }])[-200:]   # keep last 200 trades per strategy
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    tracker[strategy] = record
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(tracker, fh, indent=2)
    except OSError as exc:
        print(f"[signal_engine] could not write accuracy tracker: {exc}", file=sys.stderr)
    return record


# --- Internal helpers ------------------------------------------------------


def _side_multiplier(accuracy: dict, side: str) -> float:
    """Directional self-learning: scale composite confidence by how this
    account has actually performed on this side (BUY = longs, SELL = shorts).

    Live data 2026-07-02: longs 30% win rate / PF 0.63, shorts 54% / PF 1.52
    over 62 deduped closes — the engine's long signals have been much weaker
    than its shorts. Rather than hard-gating a side (overfit risk on a small
    sample), scale sizing confidence, bounded to [0.80, 1.15], and only once
    the side has >= 20 deduped closes.

    Rows are deduped by (symbol, minute, pnl) because one position close
    credits every strategy that voted for it.
    """
    seen: set = set()
    pnls: list[float] = []
    for rec in accuracy.values():
        if not isinstance(rec, dict):
            continue
        for t in rec.get("trades", []):
            key = (t.get("symbol"), str(t.get("ts", ""))[:16],
                   round(float(t.get("pnl_pct", 0.0)), 10))
            if key in seen:
                continue
            seen.add(key)
            if t.get("signal") == side:
                pnls.append(float(t.get("pnl_pct", 0.0)))
    if len(pnls) < 20:
        return 1.0
    win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
    mult = 1.0 + (win_rate - 0.50) * 1.5   # 50% win rate → neutral
    return max(0.80, min(1.15, mult))


def _accuracy_multiplier(record: dict) -> float:
    """Self-learning weight multiplier from realised RISK-ADJUSTED performance.

    - Below MIN_TRADES_TO_JUDGE closed trades → neutral (1.0).
    - Win rate below AUTO_BENCH_WINRATE or profit factor below
      AUTO_BENCH_PROFIT_FACTOR → **benched** (0.0): the strategy stops
      contributing to consensus until it recovers.
    - Otherwise scaled toward EDGE_WEIGHT_MAX by the strategy's per-trade
      Sharpe-like ratio (mean pnl / pnl stdev). Two strategies with the
      same win rate are no longer equal: the one whose wins come with
      smaller variance earns more voting weight.
    """
    if not getattr(config, "SELF_LEARNING_ENABLED", True):
        return 1.0
    # Judge the ROLLING WINDOW, not lifetime totals: a strategy that fixed
    # itself can earn its way back off the bench, and one that decayed can't
    # coast on months-old wins. Lifetime counters remain for reporting only.
    window = getattr(config, "ROLLING_LEARNING_WINDOW", 50)
    recent = (record.get("trades") or [])[-window:]
    if recent:
        wins = sum(1 for t in recent if float(t.get("pnl_pct", 0.0)) > 0)
        losses = len(recent) - wins
    else:   # legacy records without a trades list — fall back to counters
        wins = int(record.get("wins", 0))
        losses = int(record.get("losses", 0))
    total = wins + losses
    min_trades = getattr(config, "MIN_TRADES_TO_JUDGE", config.ACCURACY_MIN_TRADES)
    if total < min_trades:
        return 1.0
    win_rate = wins / total
    pf = _profit_factor(recent if recent else record.get("trades", []))

    if win_rate < getattr(config, "AUTO_BENCH_WINRATE", 0.40) or \
       pf < getattr(config, "AUTO_BENCH_PROFIT_FACTOR", 0.9):
        # Probation, not execution: a zero weight freezes the record forever
        # (benched voters are never credited on new closes, so they can never
        # redeem themselves). A small exploration weight keeps evidence
        # flowing — standard bandit exploration/exploitation practice.
        return float(getattr(config, "PROBATION_WEIGHT", 0.2))

    pnls = [float(t.get("pnl_pct", 0.0)) for t in (recent or record.get("trades", []))]
    mean = sum(pnls) / len(pnls) if pnls else 0.0
    var = (sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)) if len(pnls) > 1 else 0.0
    std = var ** 0.5
    if std <= 1e-12:
        sharpe_like = 1.5 if mean > 0 else 0.0
    else:
        sharpe_like = mean / std
    # Map per-trade Sharpe [0 .. 1.5] onto [1.0 .. EDGE_WEIGHT_MAX]; negative
    # risk-adjusted performance that survived the bench gates stays at 1.0.
    frac = max(0.0, min(1.0, sharpe_like / 1.5))
    mult = 1.0 + frac * (getattr(config, "EDGE_WEIGHT_MAX", 2.0) - 1.0)
    return float(max(0.0, min(getattr(config, "EDGE_WEIGHT_MAX", 2.0), mult)))


def _profit_factor(trades: list[dict]) -> float:
    """Gross profit / gross loss from a strategy's recorded trade pnl_pct."""
    gains = sum(float(t.get("pnl_pct", 0.0)) for t in trades if float(t.get("pnl_pct", 0.0)) > 0)
    losses = -sum(float(t.get("pnl_pct", 0.0)) for t in trades if float(t.get("pnl_pct", 0.0)) < 0)
    if losses <= 0:
        return 2.0 if gains > 0 else 1.0
    return gains / losses


def apply_optimized_params() -> dict:
    """Load models/optimized_params.json and push the best tuned params onto
    each strategy module (self-tuning). Picks, per strategy, the parameter set
    with the highest out-of-sample Sharpe across symbols. Returns what it set.
    """
    if not getattr(config, "APPLY_OPTIMIZED_PARAMS", True):
        return {}
    path = os.path.join(_ROOT, config.OPTIMIZED_PARAMS_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            opt = json.load(fh)
    except (OSError, ValueError):
        return {}
    applied: dict[str, dict] = {}
    name_to_mod = {getattr(m, "NAME", ""): m for m in STRATEGIES}
    for strat, by_symbol in opt.items():
        mod = name_to_mod.get(strat)
        if not mod or not isinstance(by_symbol, dict):
            continue
        best_params, best_sharpe = None, float("-inf")
        for _sym, entry in by_symbol.items():
            best = (entry or {}).get("best") or {}
            sharpe = best.get("sharpe_annualized")
            params = best.get("params")
            if params and sharpe is not None and sharpe > best_sharpe:
                best_sharpe, best_params = sharpe, params
        if best_params:
            for k, v in best_params.items():
                if hasattr(mod, k):
                    setattr(mod, k, v)
            applied[strat] = best_params
    return applied


# Apply tuned params at import so every run uses the latest optimization.
try:
    apply_optimized_params()
except Exception as _exc:  # noqa: BLE001
    print(f"[signal_engine] apply_optimized_params skipped: {_exc}", file=sys.stderr)


def _load_accuracy_tracker() -> dict[str, Any]:
    path = os.path.join(_ROOT, config.ACCURACY_TRACKER_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _compose(signal: str, confidence: float, reason: str,
             components: list[dict], entry_price, stop, tp,
             opening_strategies: list[str] | None = None) -> dict[str, Any]:
    return {
        "signal": signal,
        "confidence": float(confidence),
        "reason": reason,
        "strategy": "composite",
        "entry_price": entry_price,
        "stop_loss": stop,
        "take_profit": tp,
        "opening_strategies": opening_strategies or [],
        "components": components,
    }


def _hold(reason: str) -> dict[str, Any]:
    return _compose("HOLD", 0.0, reason, [], None, None, None, [])


def check_correlation_exposure(symbol: str, candidate_notional: float,
                               positions: list[dict], equity: float,
                               daily_bars_by_symbol: dict[str, list]) -> tuple[bool, str]:
    """Block an entry that would over-concentrate a correlated cluster.

    Groups the candidate with any held symbol whose daily-return correlation
    is >= ``config.CORRELATION_THRESHOLD``. If the candidate's notional plus the
    cluster's existing |exposure| exceeds ``config.MAX_CLUSTER_EXPOSURE_PCT`` of
    equity, the trade is blocked.

    Fails OPEN: if correlations can't be computed (missing data), returns OK.
    """
    if equity <= 0 or not positions:
        return True, "no existing exposure to cluster"
    cand_returns = _returns(daily_bars_by_symbol.get(symbol.upper(), []))
    if cand_returns is None:
        return True, "candidate return history unavailable (fail-open)"

    cluster_exposure = 0.0
    cluster_members = []
    for p in positions:
        psym = p["symbol"].upper()
        if psym == symbol.upper():
            cluster_exposure += abs(float(p.get("market_value", 0.0)))
            cluster_members.append(psym)
            continue
        p_returns = _returns(daily_bars_by_symbol.get(psym, []))
        if p_returns is None:
            continue
        corr = _correlation(cand_returns, p_returns)
        if corr is not None and abs(corr) >= config.CORRELATION_THRESHOLD:
            cluster_exposure += abs(float(p.get("market_value", 0.0)))
            cluster_members.append(f"{psym}(corr={corr:.2f})")

    cluster_cap = config.MAX_CLUSTER_EXPOSURE_PCT * equity
    projected = cluster_exposure + candidate_notional
    if projected > cluster_cap + 1e-6:
        return False, (
            f"correlation-cluster cap: {symbol} + [{', '.join(cluster_members) or 'none'}] "
            f"would be ${projected:,.2f} ({projected/equity*100:.1f}% of equity), "
            f"cap {config.MAX_CLUSTER_EXPOSURE_PCT*100:.0f}% (${cluster_cap:,.2f})"
        )
    return True, (
        f"cluster exposure ${projected:,.2f} within "
        f"{config.MAX_CLUSTER_EXPOSURE_PCT*100:.0f}% cap"
    )


def _returns(bars: list[dict]):
    """Daily simple returns from a bars list, capped to the config lookback."""
    closes = [float(b["c"]) for b in bars if "c" in b]
    if len(closes) < 5:
        return None
    closes = closes[-config.CORRELATION_LOOKBACK_DAYS:]
    arr = np.array(closes, dtype=float)
    rets = np.diff(arr) / arr[:-1]
    return rets


def _correlation(a, b):
    """Pearson correlation over the overlapping tail of two return series."""
    n = min(len(a), len(b))
    if n < 5:
        return None
    a2, b2 = a[-n:], b[-n:]
    if np.std(a2) == 0 or np.std(b2) == 0:
        return None
    return float(np.corrcoef(a2, b2)[0, 1])


# --- CLI entry point for quick dry-run inspection --------------------------


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run signal engine on a symbol (dry-run).")
    parser.add_argument("symbol", help="ticker to analyse")
    args = parser.parse_args()

    # Pull data using existing research.py helpers
    import research  # type: ignore  # adjacent script
    bars_1m = research.get_bars(args.symbol, timeframe="1Min", limit=200)
    bars_5m = research.get_bars(args.symbol, timeframe="5Min", limit=200)
    bars_1d = research.get_bars(args.symbol, timeframe="1Day", limit=120)
    account = research.get_account()
    positions = research.get_positions()
    spy_bars_1d = bars_1d if args.symbol.upper() == "SPY" else research.get_bars("SPY", limit=120)

    regime = market_regime.classify(spy_bars_1d)
    signals = run_all_strategies(args.symbol, bars_1m, bars_5m, bars_1d, account, positions)
    composite = aggregate_signals(signals, regime["regime"], regime["weights"])
    print(json.dumps({
        "symbol": args.symbol.upper(),
        "regime": regime,
        "components": [
            {"strategy": s["strategy"], "signal": s["signal"],
             "confidence": s["confidence"], "reason": s["reason"]}
            for s in signals
        ],
        "composite": {k: v for k, v in composite.items() if k != "components"},
    }, indent=2))
