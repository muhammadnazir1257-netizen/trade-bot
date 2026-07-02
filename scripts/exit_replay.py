#!/usr/bin/env python3
"""Replay real historical entries through OLD vs NEW exit models.

The 2026-07-02 exit overhaul (breakeven at +1R, trail arms at +1R at
1.5xATR — replacing the always-on 0.5xATR trail) was justified by exit
distribution stats (57 stops vs 10 profit-targets). This script validates it
directly: take every equity entry the bot actually made (from the intraday
logs), fetch the historical 5-minute bars that followed, and simulate both
exit ladders on identical entries.

Assumptions (applied identically to both models, so the COMPARISON is fair
even where absolute numbers are approximate):
* Fill at the recorded limit price.
* R = entry x STOP_LOSS_PCT (strategy-specific stops aren't recoverable
  from the logs after meta cleanup).
* Take-profit at 3R. Time stop 90 min if favorable < 0.5%. EOD flat 15:30 ET.
* Intrabar: if a bar's range hits both stop and target, the stop is assumed
  first (conservative).
* ATR(14) computed from the 5-min bars preceding entry.
* Crypto entries are skipped (different close mechanics, tiny sample).

CLI:
    python scripts/exit_replay.py            # summary comparison
    python scripts/exit_replay.py --trades   # plus per-trade rows
"""

from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import numpy as np  # noqa: E402

import config      # noqa: E402
import research    # noqa: E402
import indicators as ind  # noqa: E402


def _log(msg: str) -> None:
    print(f"[exit_replay] {msg}", file=sys.stderr)


# --- Harvest real entries from the logs -------------------------------------


def harvest_entries() -> list[dict[str, Any]]:
    """Every equity entry the bot actually placed, with fill approximations."""
    entries: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(_ROOT, config.INTRADAY_LOG_DIR, "2*.jsonl"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
        except (OSError, ValueError):
            continue
        for row in rows:
            orders = {o.get("id"): o for o in row.get("orders_placed_this_iteration", []) if o}
            for s in row.get("signals", []):
                act = s.get("action_taken") or ""
                if act not in ("ORDER_PLACED", "SHORT_OPENED"):
                    continue
                symbol = s.get("symbol", "")
                if research.is_crypto(symbol):
                    continue
                order = orders.get(s.get("order_id")) or {}
                limit_price = float(order.get("limit_price") or 0.0)
                if limit_price <= 0:
                    continue
                entries.append({
                    "symbol": symbol.upper(),
                    "ts": row.get("timestamp", ""),
                    "direction": "short" if act == "SHORT_OPENED" else "long",
                    "entry": limit_price,
                    "confidence": float(s.get("composite_confidence") or 0.0),
                    "regime": row.get("regime", "?"),
                })
    return entries


# --- Historical bars for a specific window ----------------------------------


def fetch_bars_window(symbol: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """5-min bars for [start, end], ascending. Direct data-API call because
    research.get_bars only serves the most-recent window."""
    try:
        data = research._request(  # noqa: SLF001 — same package, reuse auth/retry
            research.DATA_BASE,
            f"/v2/stocks/{symbol}/bars",
            params={
                "timeframe": "5Min",
                "start": start_iso,
                "end": end_iso,
                "limit": 500,
                "sort": "asc",
                "feed": research.DATA_FEED,
                "adjustment": "raw",
            },
        )
        return data.get("bars") or []
    except RuntimeError as exc:
        _log(f"bars fetch failed {symbol} {start_iso[:10]}: {exc}")
        return []


# --- Exit-ladder simulation ---------------------------------------------------


MODELS: dict[str, dict[str, float | None]] = {
    # trail_mult × ATR; activation_r = favorable R before trail arms;
    # breakeven_r = favorable R at which stop jumps to entry (None = never).
    "old": {"trail_mult": 0.5, "activation_r": 0.0, "breakeven_r": None},
    "new": {"trail_mult": 1.5, "activation_r": 1.0, "breakeven_r": 1.0},
}


def simulate(entry: float, direction: str, bars: list[dict[str, Any]],
             atr: float, model: str | dict) -> dict[str, Any]:
    """Walk bars forward applying one exit ladder. Returns exit info."""
    is_long = direction == "long"
    r_unit = entry * config.STOP_LOSS_PCT
    hard_stop = entry - r_unit if is_long else entry + r_unit
    tp = entry + 3 * r_unit if is_long else entry - 3 * r_unit

    params = MODELS[model] if isinstance(model, str) else model
    trail_mult = float(params["trail_mult"])
    activation_r = float(params["activation_r"])
    breakeven_r = params.get("breakeven_r")
    breakeven = breakeven_r is not None

    peak = entry           # best price seen in our favor
    trailing: float | None = None
    entry_dt = None

    def _pnl(px: float) -> float:
        return (px - entry) / entry if is_long else (entry - px) / entry

    for i, b in enumerate(bars):
        hi, lo, close = float(b["h"]), float(b["l"]), float(b["c"])
        t = b.get("t", "")
        if entry_dt is None:
            entry_dt = t

        # Update favorable extreme and ladders (using bar extremes)
        fav_extreme = hi if is_long else lo
        peak = max(peak, hi) if is_long else min(peak, lo)
        fav_dist = (peak - entry) if is_long else (entry - peak)

        if breakeven and fav_dist >= float(breakeven_r) * r_unit:
            hard_stop = max(hard_stop, entry) if is_long else min(hard_stop, entry)
        if atr > 0 and fav_dist >= activation_r * r_unit:
            new_trail = peak - trail_mult * atr if is_long else peak + trail_mult * atr
            if trailing is None:
                trailing = new_trail
            else:
                trailing = max(trailing, new_trail) if is_long else min(trailing, new_trail)

        stop_level = hard_stop
        if trailing is not None:
            stop_level = max(hard_stop, trailing) if is_long else min(hard_stop, trailing)

        # Conservative intrabar ordering: stop first, then target.
        if (is_long and lo <= stop_level) or (not is_long and hi >= stop_level):
            return {"exit": "STOP", "price": stop_level, "pnl": _pnl(stop_level), "bars_held": i + 1}
        if (is_long and hi >= tp) or (not is_long and lo <= tp):
            return {"exit": "PROFIT", "price": tp, "pnl": _pnl(tp), "bars_held": i + 1}

        # Time stop: 90 min = 18 five-minute bars
        if i + 1 >= config.TIME_STOP_MINUTES // 5 and _pnl(close) < config.TIME_STOP_MIN_MOVE_PCT:
            return {"exit": "TIME", "price": close, "pnl": _pnl(close), "bars_held": i + 1}

    last = float(bars[-1]["c"]) if bars else entry
    return {"exit": "EOD", "price": last, "pnl": _pnl(last), "bars_held": len(bars)}


# --- Runner -------------------------------------------------------------------


def prepare_data() -> list[dict[str, Any]]:
    """Fetch bars once per entry; reusable across every exit-model variant."""
    entries = harvest_entries()
    _log(f"harvested {len(entries)} equity entries from logs")
    prepared = []
    for e in entries:
        ts = e["ts"]
        try:
            t0 = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # Pre-entry window for ATR + post-entry window to EOD (21:00 UTC covers
        # 16:00/17:00 ET across DST).
        pre_start = (t0 - timedelta(hours=6)).isoformat()
        day_end = t0.replace(hour=21, minute=30, second=0).isoformat()
        bars = fetch_bars_window(e["symbol"], pre_start, day_end)
        if len(bars) < 20:
            _log(f"skip {e['symbol']} {ts[:16]}: only {len(bars)} bars")
            continue
        pre = [b for b in bars if b["t"] <= ts]
        post = [b for b in bars if b["t"] > ts]
        if len(pre) < config.ATR_PERIOD + 2 or len(post) < 3:
            _log(f"skip {e['symbol']} {ts[:16]}: pre={len(pre)} post={len(post)}")
            continue
        highs = np.array([float(b["h"]) for b in pre])
        lows = np.array([float(b["l"]) for b in pre])
        closes = np.array([float(b["c"]) for b in pre])
        atr_arr = ind.atr(highs, lows, closes, period=config.ATR_PERIOD)
        atr = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else e["entry"] * 0.002
        prepared.append({**e, "post": post, "atr": atr})
    return prepared


def _stats(sims: list[dict[str, Any]]) -> dict[str, Any]:
    if not sims:
        return {}
    from collections import Counter
    pnls = [s["pnl"] for s in sims]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "n": len(pnls),
        "win_rate": len(wins) / len(pnls),
        "avg_pnl_pct": sum(pnls) / len(pnls) * 100,
        "cum_pnl_pct": sum(pnls) * 100,
        "avg_win_pct": (sum(wins) / len(wins) * 100) if wins else 0.0,
        "avg_loss_pct": (sum(losses) / len(losses) * 100) if losses else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) < 0 else None,
        "exits": dict(Counter(s["exit"] for s in sims)),
    }


def run_replay(show_trades: bool = False,
               prepared: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    prepared = prepared if prepared is not None else prepare_data()
    results: dict[str, list] = {"old": [], "new": []}
    rows = []
    for p in prepared:
        old = simulate(p["entry"], p["direction"], p["post"], p["atr"], "old")
        new = simulate(p["entry"], p["direction"], p["post"], p["atr"], "new")
        results["old"].append(old)
        results["new"].append(new)
        rows.append({k: p[k] for k in ("symbol", "ts", "direction", "entry")}
                    | {"old": old, "new": new})
    return {"old": _stats(results["old"]), "new": _stats(results["new"]),
            "trades": rows if show_trades else []}


def run_grid(variants: dict[str, dict], prepared: list[dict[str, Any]] | None = None,
             split_date: str = "2026-06-15") -> dict[str, Any]:
    """Run every variant over the same prepared entries, with a robustness
    split (trending first half vs ranging second half)."""
    prepared = prepared if prepared is not None else prepare_data()
    out: dict[str, Any] = {}
    for name, params in variants.items():
        sims_all, sims_a, sims_b = [], [], []
        for p in prepared:
            s = simulate(p["entry"], p["direction"], p["post"], p["atr"], params)
            sims_all.append(s)
            (sims_a if p["ts"][:10] < split_date else sims_b).append(s)
        out[name] = {"all": _stats(sims_all),
                     "pre_split": _stats(sims_a), "post_split": _stats(sims_b),
                     "params": params}
    return out


def render(rep: dict[str, Any]) -> str:
    lines = ["=== EXIT MODEL REPLAY: real entries, historical bars ===", ""]
    for name, label in (("old", "OLD (0.5xATR trail from entry)"),
                        ("new", "NEW (breakeven@1R, 1.5xATR trail arms@1R)")):
        s = rep[name]
        if not s:
            lines.append(f"{label}: no data")
            continue
        pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") else "n/a"
        lines += [
            f"-- {label} --",
            f"N={s['n']}  win rate {s['win_rate']:.0%}  avg {s['avg_pnl_pct']:+.3f}%/trade  "
            f"cum {s['cum_pnl_pct']:+.2f}%",
            f"avg win {s['avg_win_pct']:+.3f}%  avg loss {s['avg_loss_pct']:+.3f}%  PF {pf}",
            f"exits: {s['exits']}",
            "",
        ]
    for t in rep.get("trades", []):
        lines.append(
            f"{t['ts'][:16]} {t['symbol']:<6} {t['direction']:<5} @{t['entry']:<8.2f} "
            f"old={t['old']['exit']:<6}{t['old']['pnl']*100:+.2f}%  "
            f"new={t['new']['exit']:<6}{t['new']['pnl']*100:+.2f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Replay entries through old vs new exit models.")
    parser.add_argument("--trades", action="store_true", help="show per-trade rows")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(render(run_replay(show_trades=args.trades)))
