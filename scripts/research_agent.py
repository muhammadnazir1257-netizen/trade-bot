"""Continuous strategy research: backtests, parameter tuning, weekly report.

Designed to run weekends and pre-market — the intraday loop should not call
into the heavy paths (``backtest_strategy`` / ``tune_strategy_params``)
because they each replay tens of thousands of bars.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import config            # noqa: E402
import indicators as ind  # noqa: E402
import research          # type: ignore  # noqa: E402
from strategies import STRATEGIES, market_regime  # noqa: E402


# --- Persistence helpers ---------------------------------------------------


def _path(rel: str) -> str:
    return os.path.join(_ROOT, rel)


def _load(rel: str) -> dict:
    try:
        with open(_path(rel), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(rel: str, data: dict) -> None:
    os.makedirs(os.path.dirname(_path(rel)), exist_ok=True)
    with open(_path(rel), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# --- News research ---------------------------------------------------------


STRATEGY_RESEARCH_QUERIES = (
    "algorithmic trading strategies 2025",
    "quantitative intraday trading",
    "backtest strategy performance",
)


def fetch_strategy_papers() -> str:
    """Pull recent news matching strategy-research queries and append to
    ``research/strategy_notes.md``. Returns the appended block as a string."""
    items: list[dict] = []
    # Alpaca's news endpoint doesn't support free-text search — best we can do
    # is pull general market news; we filter headlines against our queries.
    try:
        general = research.get_news("SPY", limit=50)
    except Exception:
        general = []
    keywords = [k.lower() for k in
                ["algorithm", "quant", "intraday", "strategy", "backtest",
                 "machine learning", "vwap", "momentum", "mean reversion"]]
    for n in general:
        text = f"{n.get('headline','')} {n.get('summary','')}".lower()
        if any(k in text for k in keywords):
            items.append(n)

    notes_dir = _path("research")
    os.makedirs(notes_dir, exist_ok=True)
    notes_path = os.path.join(notes_dir, "strategy_notes.md")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    block_lines = [f"\n## {today} — strategy-research scan\n"]
    if not items:
        block_lines.append("- No strategy-relevant headlines found in this pass.")
    for n in items[:20]:
        date_part = (n.get("created_at") or "")[:10]
        block_lines.append(
            f"- [{date_part}] **{n.get('headline','(no headline)')}** "
            f"({n.get('source','')}) — {(n.get('summary') or '')[:240]}"
        )
    block = "\n".join(block_lines) + "\n"
    try:
        with open(notes_path, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as exc:
        print(f"[research_agent] could not append to strategy notes: {exc}", file=sys.stderr)
    return block


# --- Backtesting ----------------------------------------------------------


def backtest_strategy(strategy_module, symbol: str, lookback_days: int = 90) -> dict[str, Any]:
    """Replay 5-minute bars through a strategy and compute performance stats.

    The simulator opens a position on BUY / SELL, exits at the strategy's
    declared ``take_profit`` or ``stop_loss``, or at the end of the test
    window. No compounding — each trade is a fixed 1 share unit so PnL is
    in price-points (later divided by entry price for % return).
    """
    bars = research.get_bars(symbol, timeframe="5Min", limit=min(10000, lookback_days * 78))
    if not bars or len(bars) < 200:
        return {"symbol": symbol, "strategy": getattr(strategy_module, "NAME", "?"),
                "error": "insufficient bars"}

    # Need both 1m and 1d for some strategies — pull a reasonable slice
    bars_1d = research.get_bars(symbol, timeframe="1Day", limit=120)
    bars_1m = research.get_bars(symbol, timeframe="1Min", limit=400)

    trades: list[dict] = []
    open_trade: dict | None = None
    min_warmup = 60

    for i in range(min_warmup, len(bars)):
        window_5m = bars[: i + 1]
        # Light per-iteration data
        try:
            sig = strategy_module.analyze(symbol, bars_1m, window_5m, bars_1d, {}, [])
        except Exception:
            continue

        last_close = float(window_5m[-1].get("c", 0))

        # Exit logic on any open trade
        if open_trade:
            stop = open_trade["stop"]
            tp = open_trade["tp"]
            high = float(window_5m[-1].get("h", last_close))
            low = float(window_5m[-1].get("l", last_close))
            if open_trade["side"] == "BUY":
                if low <= stop:
                    open_trade.update({"exit_index": i, "exit_price": stop,
                                       "reason": "stop"})
                    trades.append(open_trade); open_trade = None
                elif high >= tp:
                    open_trade.update({"exit_index": i, "exit_price": tp,
                                       "reason": "take_profit"})
                    trades.append(open_trade); open_trade = None
            else:  # SELL / short
                if high >= stop:
                    open_trade.update({"exit_index": i, "exit_price": stop,
                                       "reason": "stop"})
                    trades.append(open_trade); open_trade = None
                elif low <= tp:
                    open_trade.update({"exit_index": i, "exit_price": tp,
                                       "reason": "take_profit"})
                    trades.append(open_trade); open_trade = None

        # Entry logic — only enter if flat
        if open_trade is None and sig.get("signal") in ("BUY", "SELL") \
                and sig.get("entry_price") and sig.get("stop_loss") and sig.get("take_profit"):
            open_trade = {
                "side": sig["signal"],
                "entry_index": i,
                "entry_price": float(sig["entry_price"]),
                "stop": float(sig["stop_loss"]),
                "tp": float(sig["take_profit"]),
                "confidence": float(sig.get("confidence", 0.0)),
            }

    # Force-close any open trade at last close
    if open_trade and len(bars) > open_trade["entry_index"]:
        open_trade.update({"exit_index": len(bars) - 1,
                           "exit_price": float(bars[-1]["c"]),
                           "reason": "session_end"})
        trades.append(open_trade)

    if not trades:
        return {"symbol": symbol,
                "strategy": getattr(strategy_module, "NAME", "?"),
                "trades": 0, "win_rate": None, "profit_factor": None}

    pnl = []
    for t in trades:
        if t["side"] == "BUY":
            r = (t["exit_price"] - t["entry_price"]) / t["entry_price"]
        else:
            r = (t["entry_price"] - t["exit_price"]) / t["entry_price"]
        t["return_pct"] = r
        pnl.append(r)
    pnl_arr = np.array(pnl)
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr <= 0]
    win_rate = float(len(wins) / len(pnl_arr))
    avg_win = float(np.mean(wins)) if len(wins) else 0.0
    avg_loss = float(np.mean(losses)) if len(losses) else 0.0
    gross_p = float(np.sum(wins))
    gross_l = float(-np.sum(losses))
    profit_factor = gross_p / gross_l if gross_l > 0 else float("inf") if gross_p > 0 else 0.0
    # Max drawdown over the equity curve
    equity = np.cumsum(pnl_arr)
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = float(np.max(drawdown)) if len(drawdown) else 0.0
    # Sharpe-ish — annualized assuming 5m bars and 78 bars/day, 252 trading days
    if pnl_arr.std(ddof=0) > 0:
        per_trade_sharpe = pnl_arr.mean() / pnl_arr.std(ddof=0)
        sharpe = per_trade_sharpe * math.sqrt(252)
    else:
        sharpe = 0.0

    return {
        "symbol": symbol,
        "strategy": getattr(strategy_module, "NAME", "?"),
        "lookback_days_requested": lookback_days,
        "bars_used": len(bars),
        "trades": len(trades),
        "win_rate": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "sharpe_annualized": float(sharpe),
        "total_return_pct": float(np.sum(pnl_arr)),
    }


def backtest_all() -> dict[str, Any]:
    """Run backtests for every strategy × every watchlist symbol."""
    wl = _load(config.WATCHLIST_PATH).get("watchlist", [])
    results: dict[str, dict[str, dict]] = {}
    for entry in wl:
        sym = entry["symbol"]
        results.setdefault(sym, {})
        for mod in STRATEGIES:
            try:
                r = backtest_strategy(mod, sym)
                results[sym][mod.NAME] = r
            except Exception as exc:  # noqa: BLE001
                results[sym][mod.NAME] = {"error": str(exc)}
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(),
               "results": results}
    _save(config.BACKTEST_RESULTS_PATH, payload)
    return payload


# --- Parameter tuning -----------------------------------------------------


# Grid hooks: each tunable strategy declares a small param grid below.
# These wrappers patch a module-level constant temporarily for the search.

_PARAM_GRIDS = {
    "vwap_reversion": [
        {"BAND_STD_MULT": 1.0, "RSI_OVERSOLD": 30, "RSI_OVERBOUGHT": 70},
        {"BAND_STD_MULT": 1.5, "RSI_OVERSOLD": 35, "RSI_OVERBOUGHT": 65},
        {"BAND_STD_MULT": 2.0, "RSI_OVERSOLD": 40, "RSI_OVERBOUGHT": 60},
        {"BAND_STD_MULT": 2.5, "RSI_OVERSOLD": 30, "RSI_OVERBOUGHT": 70},
    ],
    "momentum_breakout": [
        {"VOLUME_MULT_TRIGGER": 1.3},
        {"VOLUME_MULT_TRIGGER": 1.5},
        {"VOLUME_MULT_TRIGGER": 1.8},
        {"VOLUME_MULT_TRIGGER": 2.0},
    ],
    "rsi_divergence": [
        {"VOLUME_CONFIRM_MULT": 1.0, "MIN_DIVERGENCE_SPAN": 4},
        {"VOLUME_CONFIRM_MULT": 1.2, "MIN_DIVERGENCE_SPAN": 5},
        {"VOLUME_CONFIRM_MULT": 1.5, "MIN_DIVERGENCE_SPAN": 6},
    ],
    "squeeze_momentum": [
        {"MIN_SQUEEZE_DURATION": 3},
        {"MIN_SQUEEZE_DURATION": 4},
        {"MIN_SQUEEZE_DURATION": 6},
        {"MIN_SQUEEZE_DURATION": 8},
    ],
}


def tune_strategy_params(strategy_module, symbol: str) -> dict[str, Any]:
    """Grid-search the registered params for a strategy on a single symbol.

    Returns the best parameter set by Sharpe ratio. If the strategy has no
    registered grid, returns ``{"error": "no grid"}``.
    """
    name = strategy_module.NAME
    grid = _PARAM_GRIDS.get(name)
    if not grid:
        return {"strategy": name, "symbol": symbol, "error": "no_grid_defined"}

    saved = {k: getattr(strategy_module, k) for k in grid[0].keys()
             if hasattr(strategy_module, k)}
    best: dict[str, Any] | None = None
    runs: list[dict[str, Any]] = []
    try:
        for params in grid:
            for k, v in params.items():
                if hasattr(strategy_module, k):
                    setattr(strategy_module, k, v)
            r = backtest_strategy(strategy_module, symbol, lookback_days=60)
            r["params"] = params
            runs.append(r)
            if best is None or (r.get("sharpe_annualized", 0) or 0) > (best.get("sharpe_annualized", 0) or 0):
                best = r
    finally:
        for k, v in saved.items():
            setattr(strategy_module, k, v)

    out = _load(config.OPTIMIZED_PARAMS_PATH)
    out.setdefault(name, {})[symbol] = {
        "best": best,
        "runs": runs,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(config.OPTIMIZED_PARAMS_PATH, out)
    return {"strategy": name, "symbol": symbol, "best": best, "runs": runs}


# --- Weekly report --------------------------------------------------------


def generate_weekly_report() -> str:
    """Compile a weekly performance + regime report.

    Reads accuracy_tracker + backtest_results, classifies the current SPY
    regime, and writes ``research/weekly_report_YYYY-MM-DD.md``.
    """
    accuracy = _load(config.ACCURACY_TRACKER_PATH)
    backtests = _load(config.BACKTEST_RESULTS_PATH)
    spy_daily = research.get_bars("SPY", timeframe="1Day", limit=120)
    regime = market_regime.classify(spy_daily)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Weekly Strategy Report — {today}", ""]
    lines.append(f"**Market regime (SPY):** {regime['regime']}")
    lines.append(f"  — {regime.get('details','')}")
    lines.append("")

    lines.append("## Strategy accuracy (live trades)")
    if not accuracy:
        lines.append("- No live trade outcomes recorded yet.")
    else:
        for strat, rec in sorted(accuracy.items()):
            w, l = int(rec.get("wins", 0)), int(rec.get("losses", 0))
            t = w + l
            wr = f"{w/t:.1%}" if t else "n/a"
            lines.append(f"- **{strat}**: {w}W / {l}L ({wr}); cumulative pnl_sum {rec.get('pnl_sum',0):.3f}")
    lines.append("")

    lines.append("## Backtest results")
    if not backtests.get("results"):
        lines.append("- No backtest results available — run `python scripts/research_agent.py backtest`.")
    else:
        for sym, by_strategy in backtests["results"].items():
            lines.append(f"### {sym}")
            for strat, r in by_strategy.items():
                if "error" in r:
                    lines.append(f"  - {strat}: ERROR ({r['error']})")
                    continue
                lines.append(
                    f"  - {strat}: {r.get('trades',0)} trades, "
                    f"win {r.get('win_rate',0):.1%}, "
                    f"PF {r.get('profit_factor',0):.2f}, "
                    f"Sharpe {r.get('sharpe_annualized',0):.2f}"
                )
        lines.append("")

    lines.append("## Recommendation")
    lines.append(
        f"- Regime is **{regime['regime']}** → emphasise: "
        + ", ".join(sorted(regime["weights"].keys())) if regime["weights"]
        else "- No regime-specific overrides this week."
    )

    notes_dir = _path("research")
    os.makedirs(notes_dir, exist_ok=True)
    path = os.path.join(notes_dir, f"weekly_report_{today}.md")
    body = "\n".join(lines) + "\n"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        print(f"[research_agent] could not write weekly report: {exc}", file=sys.stderr)
    return body


# --- CLI -------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Research / backtest / tune.")
    parser.add_argument("action", choices=("news", "backtest", "backtest-all",
                                            "tune", "weekly"))
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--strategy", default=None)
    args = parser.parse_args()

    if args.action == "news":
        print(fetch_strategy_papers())
    elif args.action == "backtest":
        if not args.symbol or not args.strategy:
            print("--symbol and --strategy are required", file=sys.stderr)
            sys.exit(2)
        mod = next((m for m in STRATEGIES if m.NAME == args.strategy), None)
        if not mod:
            print(f"unknown strategy: {args.strategy}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(backtest_strategy(mod, args.symbol), indent=2))
    elif args.action == "backtest-all":
        print(json.dumps(backtest_all(), indent=2)[:2000])
    elif args.action == "tune":
        if not args.symbol or not args.strategy:
            print("--symbol and --strategy are required", file=sys.stderr)
            sys.exit(2)
        mod = next((m for m in STRATEGIES if m.NAME == args.strategy), None)
        if not mod:
            print(f"unknown strategy: {args.strategy}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(tune_strategy_params(mod, args.symbol), indent=2))
    elif args.action == "weekly":
        print(generate_weekly_report())
