#!/usr/bin/env python3
"""Autonomous self-improvement loop — the program tunes its own parameters.

Design principle (learned the hard way, 2026-07-02): plausible improvements
are wrong about half the time. So this routine may only change behavior
through the evidence-gated channels:

1. EXIT LADDER — re-runs the exit-replay grid on ALL real entries to date.
   A candidate ladder replaces the current one only if it beats it in BOTH
   regime halves by a meaningful margin. Winning params are written to
   models/adaptive_params.json (whitelisted + hard-clamped by config; the
   risk floor is structurally un-overridable).
2. STRATEGY PARAMS — re-runs the walk-forward-gated tuner
   (research_agent.tune_strategy_params). Curve-fit params are rejected
   there; accepted ones flow through the existing optimized_params channel.
3. SELF-AUDIT — after any change, the full pytest suite must pass. If it
   fails, every change this run made is reverted and an alert is emailed.

Everything is logged to research/self_improve_<date>.md and emailed.
Scheduled weekly (TradeBot-SelfImprove, Sunday 12:00 ET) — one evidence
cycle per week is deliberate: the gates need fresh trades to mean anything.

CLI:
    python scripts/self_improve.py run          # full cycle
    python scripts/self_improve.py dry-run      # evaluate, change nothing
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import config      # noqa: E402
import analytics   # noqa: E402
import notify      # noqa: E402

STATE_PATH = os.path.join(_ROOT, "models", "self_improve_state.json")
MIN_NEW_TRADES = 25          # gates need fresh evidence before re-tuning
MIN_HALF_MARGIN_PCT = 0.30   # candidate must beat champion by this in EACH half


def _log(msg: str) -> None:
    print(f"[self_improve] {msg}", file=sys.stderr)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _run_tests() -> bool:
    """Full regression suite. Any change that breaks it gets reverted."""
    py = os.path.join(_ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    r = subprocess.run([py, "-m", "pytest", os.path.join(_ROOT, "tests"), "-q"],
                       capture_output=True, text=True, cwd=_ROOT, timeout=300)
    ok = r.returncode == 0
    if not ok:
        _log(f"TEST SUITE FAILED:\n{r.stdout[-2000:]}")
    return ok


# --- Exit-ladder evidence cycle ----------------------------------------------


def evaluate_exit_ladders() -> dict[str, Any]:
    """Grid the exit ladders over every real entry to date. Returns the
    champion/candidate comparison; only a both-halves win by margin counts."""
    import exit_replay as xr

    current = {
        "trail_mult": config.TRAILING_STOP_ATR_MULT,
        "activation_r": config.TRAIL_ACTIVATION_R,
        "breakeven_r": config.BREAKEVEN_AT_R,
    }
    candidates = {
        "champion(current)": dict(current),
        "tighter_0.4":  {"trail_mult": 0.4, "activation_r": 0.0, "breakeven_r": 0.5},
        "looser_0.75":  {"trail_mult": 0.75, "activation_r": 0.0, "breakeven_r": 0.5},
        "room_1.0@0.5R": {"trail_mult": 1.0, "activation_r": 0.5, "breakeven_r": 0.5},
        "classic_1.5@1R": {"trail_mult": 1.5, "activation_r": 1.0, "breakeven_r": 1.0},
    }
    prepared = xr.prepare_data()
    if len(prepared) < 40:
        return {"decision": "SKIP", "reason": f"only {len(prepared)} replayable entries (<40)"}
    grid = xr.run_grid(candidates, prepared=prepared)

    champ = grid["champion(current)"]
    best_name, best = "champion(current)", champ
    for name, r in grid.items():
        if name == "champion(current)":
            continue
        # Both-halves dominance by margin — the anti-overfit bar.
        if (r["pre_split"].get("cum_pnl_pct", -99) >=
                champ["pre_split"].get("cum_pnl_pct", 0) + MIN_HALF_MARGIN_PCT
                and r["post_split"].get("cum_pnl_pct", -99) >=
                champ["post_split"].get("cum_pnl_pct", 0) + MIN_HALF_MARGIN_PCT
                and r["all"].get("cum_pnl_pct", -99) > best["all"].get("cum_pnl_pct", 0)):
            best_name, best = name, r

    summary = {name: {"cum": r["all"].get("cum_pnl_pct"),
                      "pre": r["pre_split"].get("cum_pnl_pct"),
                      "post": r["post_split"].get("cum_pnl_pct")}
               for name, r in grid.items()}
    if best_name == "champion(current)":
        return {"decision": "KEEP", "reason": "no candidate beat the champion in both halves",
                "grid": summary, "n_entries": len(prepared)}
    return {"decision": "REPLACE", "winner": best_name,
            "params": grid[best_name]["params"], "grid": summary,
            "n_entries": len(prepared)}


def apply_exit_override(params: dict, evidence: str) -> None:
    data = _load_json(os.path.join(_ROOT, config.ADAPTIVE_PARAMS_PATH))
    overrides = data.get("overrides", {})
    overrides.update({
        "TRAILING_STOP_ATR_MULT": params["trail_mult"],
        "TRAIL_ACTIVATION_R": params["activation_r"],
        "BREAKEVEN_AT_R": params.get("breakeven_r") or 0.5,
    })
    _save_json(os.path.join(_ROOT, config.ADAPTIVE_PARAMS_PATH), {
        "overrides": overrides,
        "evidence": evidence,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "self_improve",
    })


# --- Main cycle ----------------------------------------------------------------


def run(dry_run: bool = False) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    state = _load_json(STATE_PATH)
    report_lines = [f"# Self-Improvement Cycle — {started[:10]}", ""]
    changes: list[str] = []

    # 0. Evidence freshness gate
    rep = analytics.full_report()
    n_trades = rep["trades"]["overall"]["n"]
    last_n = int(state.get("trades_at_last_run", 0))
    new_trades = n_trades - last_n
    report_lines += [f"- Closed trades: {n_trades} (+{new_trades} since last cycle)"]

    # Snapshot for rollback
    adaptive_path = os.path.join(_ROOT, config.ADAPTIVE_PARAMS_PATH)
    backup = adaptive_path + ".bak"
    had_overrides = os.path.exists(adaptive_path)
    if had_overrides:
        shutil.copy2(adaptive_path, backup)

    if new_trades < MIN_NEW_TRADES:
        report_lines += [f"- DECISION: insufficient new evidence "
                         f"({new_trades} < {MIN_NEW_TRADES} new trades) — no parameter "
                         f"changes this cycle. Analytics summary only.", ""]
        report_lines += analytics.summary_lines(rep)
    else:
        # 1. Exit-ladder evidence cycle
        exit_eval = evaluate_exit_ladders()
        report_lines += ["", "## Exit ladder", f"- decision: {exit_eval['decision']}",
                         f"- detail: {json.dumps(exit_eval.get('grid', exit_eval.get('reason')), default=str)[:800]}"]
        if exit_eval["decision"] == "REPLACE" and not dry_run:
            evidence = (f"replay {exit_eval['n_entries']} entries: {exit_eval['winner']} "
                        f"beat champion in both halves")
            apply_exit_override(exit_eval["params"], evidence)
            changes.append(f"exit ladder -> {exit_eval['winner']}")

        # 2. Walk-forward-gated strategy tuner (top symbols only — API budget)
        try:
            import research_agent as ra
            tuned = []
            for mod in ra.STRATEGIES[:4]:
                for sym in ("SPY", "QQQ"):
                    r = ra.tune_strategy_params(mod, sym)
                    best = r.get("best")
                    if best and (best.get("walk_forward") or {}).get("accepted"):
                        tuned.append(f"{r['strategy']}/{sym}")
            report_lines += ["", "## Strategy tuner (walk-forward gated)",
                             f"- accepted: {tuned or 'none — all candidates failed the gate'}"]
            if tuned:
                changes.append(f"tuned params accepted: {tuned}")
        except Exception as exc:  # noqa: BLE001
            report_lines += ["", f"## Strategy tuner: skipped ({exc})"]

    # 3. Self-audit: full test suite after any change
    if changes and not dry_run:
        if _run_tests():
            report_lines += ["", f"## Applied changes: {changes}", "- test suite: PASS"]
        else:
            # Roll back everything this cycle touched
            if had_overrides:
                shutil.copy2(backup, adaptive_path)
            elif os.path.exists(adaptive_path):
                os.remove(adaptive_path)
            report_lines += ["", "## ROLLBACK: test suite failed after changes — "
                             "all overrides reverted. Investigate before next cycle."]
            changes = ["ROLLED BACK (tests failed)"]
    elif not changes:
        report_lines += ["", "## No changes applied this cycle."]

    if not dry_run:
        state["trades_at_last_run"] = n_trades
        state["last_run"] = started
        state["last_changes"] = changes
        _save_json(STATE_PATH, state)

    # 4. Written record + email
    report = "\n".join(report_lines) + "\n"
    out_path = os.path.join(_ROOT, "research", f"self_improve_{started[:10]}.md")
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(report)
    except OSError as exc:
        _log(f"cannot write report: {exc}")
    if not dry_run:
        try:
            notify.send_email(
                f"TradeBot self-improve: {', '.join(changes) if changes else 'no changes'}",
                report)
        except Exception as exc:  # noqa: BLE001
            _log(f"email failed: {exc}")
    return {"changes": changes, "report_path": out_path, "new_trades": new_trades}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Evidence-gated self-improvement cycle.")
    parser.add_argument("action", choices=("run", "dry-run"), nargs="?", default="run")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    result = run(dry_run=(args.action == "dry-run"))
    print(json.dumps(result, indent=2, default=str))
