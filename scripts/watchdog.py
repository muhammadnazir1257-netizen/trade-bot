#!/usr/bin/env python3
"""Dead-man's switch for the trading system.

The June 2026 phantom halt sat unnoticed for four days because nothing
watches the watcher. This script closes that hole: it reads heartbeat.json
and emails an alert (via notify.send_email) when either

* the heartbeat is STALE — last_run older than WATCHDOG_STALE_MINUTES,
  meaning the Task Scheduler job died, the machine slept, or the monitor
  is crashing on startup; or
* the kill switch is ENGAGED — halted=true, so a trip gets a same-hour
  email instead of being discovered days later in the journal.

Alerts are rate-limited (one per condition per WATCHDOG_ALERT_COOLDOWN_HOURS)
via a small state file, so a weekend outage sends a handful of emails, not
hundreds. A recovery email is sent once when a previously-alerted condition
clears.

Scheduled every 30 minutes by Windows Task Scheduler (TradeBot-Watchdog).

CLI:
    python scripts/watchdog.py check
    python scripts/watchdog.py status
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _d in (_HERE, _ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import config  # noqa: E402
import notify  # noqa: E402

STATE_PATH = os.path.join(_ROOT, "intraday_log", "watchdog_state.json")

STALE_MINUTES = getattr(config, "WATCHDOG_STALE_MINUTES", 30)
COOLDOWN_HOURS = getattr(config, "WATCHDOG_ALERT_COOLDOWN_HOURS", 6)


def _log(msg: str) -> None:
    print(f"[watchdog] {msg}", file=sys.stderr)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        _log(f"cannot write state: {exc}")


def _minutes_since(iso_ts: str) -> float | None:
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _should_alert(state: dict, key: str) -> bool:
    last = state.get(f"last_alert_{key}")
    age_min = _minutes_since(last) if last else None
    return age_min is None or age_min >= COOLDOWN_HOURS * 60


def check() -> dict:
    """Evaluate heartbeat; alert on stale/halted; announce recovery."""
    hb = _load_json(os.path.join(_ROOT, config.HEARTBEAT_PATH))
    state = _load_json(STATE_PATH)
    now_iso = datetime.now(timezone.utc).isoformat()
    findings: list[str] = []

    # --- staleness ---
    age_min = _minutes_since(hb.get("last_run", ""))
    stale = age_min is None or age_min > STALE_MINUTES
    if stale:
        desc = (f"heartbeat last_run is {age_min:.0f} min old"
                if age_min is not None else "heartbeat unreadable")
        findings.append(f"STALE: {desc} (threshold {STALE_MINUTES} min)")
        if _should_alert(state, "stale"):
            notify.send_email(
                "⚠️ TradeBot watchdog: heartbeat STALE",
                f"{desc}.\n\nThe intraday monitor is not running. Check "
                f"Task Scheduler (TradeBot-Intraday), the machine's sleep "
                f"settings, and intraday_log/launcher.log.\n\n"
                f"heartbeat.json:\n{json.dumps(hb, indent=2)}")
            state["last_alert_stale"] = now_iso
        state["stale_active"] = True
    elif state.get("stale_active"):
        notify.send_email(
            "✅ TradeBot watchdog: heartbeat recovered",
            f"Heartbeat is fresh again (last_run {age_min:.0f} min ago).")
        state["stale_active"] = False

    # --- kill switch ---
    halted = bool(hb.get("halted", False))
    if halted:
        reason = hb.get("halted_reason", "?")
        findings.append(f"HALTED: {reason}")
        if _should_alert(state, "halted"):
            notify.send_email(
                "⚠️ TradeBot watchdog: kill switch ENGAGED",
                f"halted=true since {hb.get('halted_at', '?')}\n"
                f"reason: {reason}\n\n"
                f"If this is a data hiccup (see June 2026 incident), run:\n"
                f"  python scripts/kill_switch.py reset\n\n"
                f"heartbeat.json:\n{json.dumps(hb, indent=2)}")
            state["last_alert_halted"] = now_iso
        state["halted_active"] = True
    elif state.get("halted_active"):
        notify.send_email(
            "✅ TradeBot watchdog: kill switch cleared",
            "halted=false — trading can resume on the next tick.")
        state["halted_active"] = False

    state["last_check"] = now_iso
    _save_state(state)
    result = {"ok": not findings, "findings": findings,
              "heartbeat_age_min": age_min, "halted": halted}
    _log(json.dumps(result))
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading-system dead-man's switch.")
    parser.add_argument("action", choices=("check", "status"), nargs="?", default="check")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.action == "status":
        print(json.dumps(_load_json(STATE_PATH), indent=2))
    else:
        r = check()
        print(json.dumps(r, indent=2))
        raise SystemExit(0 if r["ok"] else 1)
