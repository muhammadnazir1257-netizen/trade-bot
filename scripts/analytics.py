#!/usr/bin/env python3
"""Performance analytics for the trading system.

Turns the raw exhaust the system already produces (intraday_log/*.jsonl,
models/accuracy_tracker.json, models/accuracy_archive/*.json) into the
metrics that actually tell you whether the bot has edge:

* Equity curve   — last portfolio_value per day, persisted to
                   models/equity_curve.json so other tools can chart it.
* Return metrics — total return, annualized Sharpe (trading days only),
                   max drawdown, daily volatility.
* Trade metrics  — win rate, avg win, avg loss, payoff ratio, expectancy,
                   profit factor; overall and per strategy / symbol / side.
* Exit analysis  — how positions actually die (stop / profit / time / EOD),
                   which is where the win/loss asymmetry shows up first.

CLI:
    python scripts/analytics.py report            # human-readable
    python scripts/analytics.py json              # machine-readable
    python scripts/analytics.py report --archived # include pre-retune trades
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys
from collections import defaultdict
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import config  # noqa: E402

EQUITY_CURVE_PATH = "models/equity_curve.json"
TRADING_DAYS_PER_YEAR = 252


def _log(msg: str) -> None:
    print(f"[analytics] {msg}", file=sys.stderr)


# --- Equity curve -----------------------------------------------------------


def build_equity_curve(persist: bool = True) -> list[dict[str, Any]]:
    """Last seen portfolio_value per day across every intraday log."""
    equity_by_day: dict[str, float] = {}
    for path in sorted(glob.glob(os.path.join(_ROOT, config.INTRADAY_LOG_DIR, "2*.jsonl"))):
        day = os.path.basename(path)[:10]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    pv = row.get("portfolio_value")
                    if pv:
                        equity_by_day[day] = float(pv)
        except OSError as exc:
            _log(f"cannot read {path}: {exc}")
    curve = [{"date": d, "equity": v} for d, v in sorted(equity_by_day.items())]
    if persist and curve:
        out = os.path.join(_ROOT, EQUITY_CURVE_PATH)
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(curve, fh, indent=2)
        except OSError as exc:
            _log(f"cannot persist equity curve: {exc}")
    return curve


def return_metrics(curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Total return, annualized Sharpe, max drawdown from the daily curve.

    Weekend/holiday days (equity unchanged because nothing trades) are
    dropped before computing volatility so flat days don't fake stability.
    """
    if len(curve) < 2:
        return {"total_return_pct": 0.0, "sharpe": None, "max_drawdown_pct": 0.0,
                "daily_vol_pct": None, "days": len(curve)}

    equities = [c["equity"] for c in curve]
    total_return = equities[-1] / equities[0] - 1.0

    # Daily returns on days where equity actually moved (trading days)
    rets = []
    for prev, cur in zip(equities, equities[1:]):
        if prev > 0 and cur != prev:
            rets.append(cur / prev - 1.0)

    sharpe = None
    vol = None
    if len(rets) >= 5:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var)
        if vol > 0:
            sharpe = mean / vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    # Max drawdown: worst peak-to-trough
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = min(max_dd, e / peak - 1.0)

    return {
        "total_return_pct": total_return * 100,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
        "daily_vol_pct": vol * 100 if vol is not None else None,
        "days": len(curve),
        "active_days": len(rets) + 1,
        "start_equity": equities[0],
        "end_equity": equities[-1],
    }


# --- Trade-level metrics ----------------------------------------------------


def _load_trades(include_archived: bool = False) -> list[dict[str, Any]]:
    """Flatten accuracy tracker (+ optional archive) into per-strategy trade rows."""
    rows: list[dict[str, Any]] = []

    def _ingest(data: dict, source: str) -> None:
        for strat, rec in data.items():
            for t in rec.get("trades", []) if isinstance(rec, dict) else []:
                rows.append({
                    "strategy": strat, "source": source,
                    "ts": t.get("ts", ""), "symbol": t.get("symbol", "?"),
                    "signal": t.get("signal", "?"), "outcome": t.get("outcome", "?"),
                    "pnl_pct": float(t.get("pnl_pct", 0.0)),
                })

    try:
        with open(os.path.join(_ROOT, config.ACCURACY_TRACKER_PATH), encoding="utf-8") as fh:
            _ingest(json.load(fh), "live")
    except (OSError, ValueError) as exc:
        _log(f"cannot read accuracy tracker: {exc}")

    if include_archived:
        for path in glob.glob(os.path.join(_ROOT, "models/accuracy_archive/*.json")):
            name = os.path.basename(path).split("_pre_")[0]
            try:
                with open(path, encoding="utf-8") as fh:
                    _ingest({name: json.load(fh)}, "archived")
            except (OSError, ValueError):
                continue
    return rows


def _bucket_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    n = len(trades)
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0   # negative
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else None
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "avg_win_pct": avg_win * 100,
        "avg_loss_pct": avg_loss * 100, "payoff_ratio": payoff,
        "profit_factor": pf if pf != math.inf else None,
        "expectancy_pct": expectancy * 100, "cum_pnl_pct": sum(t["pnl_pct"] for t in trades) * 100,
    }


def trade_metrics(include_archived: bool = False) -> dict[str, Any]:
    """Overall + per-strategy/symbol/side stats.

    Note: the tracker credits every strategy that voted to open a position,
    so one position close can appear under several strategies. The
    ``overall`` bucket deduplicates by (symbol, minute, pnl) so a
    multi-strategy position counts once; per-strategy buckets deliberately
    keep the duplicates (each strategy owns its vote).
    """
    rows = _load_trades(include_archived)

    seen: set = set()
    deduped = []
    for t in sorted(rows, key=lambda r: r["ts"]):
        key = (t["symbol"], t["ts"][:16], round(t["pnl_pct"], 10))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)

    per_strategy = defaultdict(list)
    per_symbol = defaultdict(list)
    per_side = defaultdict(list)
    for t in rows:
        per_strategy[t["strategy"]].append(t)
    for t in deduped:
        per_symbol[t["symbol"]].append(t)
        per_side[t["signal"]].append(t)

    return {
        "overall": _bucket_stats(deduped),
        "per_strategy": {k: _bucket_stats(v) for k, v in sorted(per_strategy.items())},
        "per_symbol": {k: _bucket_stats(v) for k, v in sorted(per_symbol.items())},
        "per_side": {k: _bucket_stats(v) for k, v in sorted(per_side.items())},
    }


# --- Exit analysis ----------------------------------------------------------


def exit_analysis() -> dict[str, int]:
    """How positions die, from the intraday logs."""
    counts: dict[str, int] = defaultdict(int)
    for path in sorted(glob.glob(os.path.join(_ROOT, config.INTRADAY_LOG_DIR, "2*.jsonl"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    for s in row.get("signals", []):
                        a = s.get("action_taken") or ""
                        if a.startswith("CLOSE_FAILED"):
                            counts["CLOSE_FAILED"] += 1
                        elif a in ("CLOSE_STOP", "CLOSE_PROFIT", "CLOSE_TIME",
                                   "CLOSE_EOD", "CLOSE_PENDING"):
                            counts[a] += 1
                        elif a in ("ORDER_PLACED", "SHORT_OPENED"):
                            counts["ENTRIES"] += 1
        except OSError:
            continue
    return dict(counts)


# --- Report -----------------------------------------------------------------


def full_report(include_archived: bool = False) -> dict[str, Any]:
    curve = build_equity_curve(persist=True)
    return {
        "equity": return_metrics(curve),
        "trades": trade_metrics(include_archived),
        "exits": exit_analysis(),
    }


def _fmt(v: Any, spec: str = ".2f", none: str = "n/a") -> str:
    return format(v, spec) if isinstance(v, (int, float)) else none


def render_text(report: dict[str, Any]) -> str:
    eq = report["equity"]
    tr = report["trades"]
    ex = report["exits"]
    o = tr["overall"]
    lines = [
        "=== PERFORMANCE REPORT ===",
        "",
        "-- Account --",
        f"Equity: ${eq.get('end_equity', 0):,.2f}  (start ${eq.get('start_equity', 0):,.2f})",
        f"Total return: {eq['total_return_pct']:+.2f}%  over {eq['days']} days"
        f" ({eq.get('active_days', '?')} active)",
        f"Sharpe (annualized): {_fmt(eq['sharpe'])}",
        f"Max drawdown: {eq['max_drawdown_pct']:.2f}%",
        f"Daily vol: {_fmt(eq['daily_vol_pct'], '.3f')}%",
        "",
        "-- Trades (deduped across strategies) --",
        f"N: {o['n']}   Win rate: {o['win_rate']:.0%}",
        f"Avg win: {o['avg_win_pct']:+.3f}%   Avg loss: {o['avg_loss_pct']:+.3f}%"
        f"   Payoff: {_fmt(o['payoff_ratio'])}",
        f"Profit factor: {_fmt(o['profit_factor'])}   "
        f"Expectancy: {o['expectancy_pct']:+.4f}%/trade",
        "",
        "-- Exits --",
    ]
    total_exits = sum(v for k, v in ex.items() if k.startswith("CLOSE_") and k != "CLOSE_FAILED")
    for k in ("CLOSE_STOP", "CLOSE_PROFIT", "CLOSE_TIME", "CLOSE_EOD", "CLOSE_PENDING"):
        if ex.get(k):
            pct = ex[k] / total_exits * 100 if total_exits else 0
            lines.append(f"{k:<14} {ex[k]:>4}  ({pct:.0f}%)")
    if ex.get("CLOSE_FAILED"):
        lines.append(f"{'CLOSE_FAILED':<14} {ex['CLOSE_FAILED']:>4}  (broker rejects — see logs)")
    lines += ["", "-- Per strategy --",
              f"{'strategy':<20}{'N':>4}{'win%':>7}{'avgW%':>8}{'avgL%':>8}{'PF':>7}{'exp%':>9}"]
    for name, s in tr["per_strategy"].items():
        lines.append(
            f"{name:<20}{s['n']:>4}{s['win_rate']:>7.0%}{s['avg_win_pct']:>8.3f}"
            f"{s['avg_loss_pct']:>8.3f}{_fmt(s['profit_factor']):>7}{s['expectancy_pct']:>9.4f}")
    lines += ["", "-- Per side --"]
    for name, s in tr["per_side"].items():
        lines.append(f"{name:<6} N={s['n']:<4} win%={s['win_rate']:.0%} "
                     f"exp={s['expectancy_pct']:+.4f}%/trade PF={_fmt(s['profit_factor'])}")
    return "\n".join(lines)


def summary_lines(report: dict[str, Any] | None = None) -> list[str]:
    """Short markdown block for the EOD journal."""
    report = report or full_report()
    eq, o = report["equity"], report["trades"]["overall"]
    return [
        f"- Total return: {eq['total_return_pct']:+.2f}% | Sharpe: {_fmt(eq['sharpe'])} | "
        f"Max DD: {eq['max_drawdown_pct']:.2f}%",
        f"- Trades: {o['n']} | Win rate: {o['win_rate']:.0%} | Payoff: {_fmt(o['payoff_ratio'])} | "
        f"PF: {_fmt(o['profit_factor'])} | Expectancy: {o['expectancy_pct']:+.4f}%/trade",
    ]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading-system performance analytics.")
    parser.add_argument("action", choices=("report", "json"), nargs="?", default="report")
    parser.add_argument("--archived", action="store_true",
                        help="include archived (pre-retune) trades")
    args = parser.parse_args()
    rep = full_report(include_archived=args.archived)
    if args.action == "json":
        print(json.dumps(rep, indent=2, default=str))
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(render_text(rep))
