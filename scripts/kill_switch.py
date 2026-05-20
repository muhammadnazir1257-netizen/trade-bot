"""Emergency controls.

* :func:`check_daily_loss` — flip the halted flag if the day's drawdown
  breaches ``config.MAX_DAILY_LOSS_PCT``.
* :func:`emergency_close_all` — submit a limit SELL at slightly below market
  for every open position. Logs the action and updates heartbeat.
* :func:`is_halted` — read the halted flag from ``heartbeat.json``.

The intraday loop calls :func:`is_halted` at the top of every iteration —
once tripped, no further trades are placed until the heartbeat is reset.
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


def _log(msg: str) -> None:
    print(f"[kill_switch] {msg}", file=sys.stderr)


def is_halted() -> bool:
    """Return True if the kill switch is currently engaged."""
    path = os.path.join(_ROOT, config.HEARTBEAT_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
        return bool(hb.get("halted", False))
    except (OSError, ValueError):
        return False


def check_daily_loss(account_start_value: float, account_current_value: float) -> dict:
    """Compute the day's drawdown and engage the kill switch if breached.

    Returns ``{"halted": bool, "loss_pct": float, "reason": str}``.
    """
    if account_start_value <= 0:
        return {"halted": False, "loss_pct": 0.0, "reason": "no start value"}
    loss_pct = (account_start_value - account_current_value) / account_start_value
    if loss_pct >= config.MAX_DAILY_LOSS_PCT:
        _set_halted(True, f"daily loss {loss_pct:.2%} >= {config.MAX_DAILY_LOSS_PCT:.2%}")
        return {"halted": True, "loss_pct": loss_pct,
                "reason": f"daily loss breach {loss_pct:.2%}"}
    return {"halted": False, "loss_pct": loss_pct,
            "reason": f"loss {loss_pct:.2%} within tolerance"}


def emergency_close_all(reason: str = "kill switch") -> list[dict]:
    """Place limit SELL orders for every open position at 0.5% below current.

    Returns the list of order responses. Never raises — failures are logged.
    """
    import trade  # type: ignore   # adjacent script, reuse Alpaca client
    placed: list[dict] = []
    try:
        positions = trade._request("GET", "/v2/positions")
    except Exception as exc:  # noqa: BLE001
        _log(f"could not fetch positions: {exc}")
        return placed

    for p in positions or []:
        symbol = p.get("symbol", "")
        qty = float(p.get("qty", 0))
        current = float(p.get("current_price", 0))
        if qty <= 0 or current <= 0:
            continue
        limit_price = round(current * 0.995, 2)   # 0.5% below market for fast fill
        try:
            order = trade.place_order(symbol, qty, "sell", limit_price)
            placed.append(order)
            _log(f"emergency close: {symbol} qty {qty} @ ${limit_price}")
        except Exception as exc:  # noqa: BLE001
            _log(f"emergency close failed for {symbol}: {exc}")
    _set_halted(True, reason)
    return placed


def reset() -> dict:
    """Clear the halted flag — call manually before the next trading day."""
    return _set_halted(False, "manual reset")


# --- Internal --------------------------------------------------------------


def _set_halted(halted: bool, reason: str) -> dict:
    path = os.path.join(_ROOT, config.HEARTBEAT_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        hb = {}
    hb["halted"] = bool(halted)
    hb["halted_reason"] = reason if halted else ""
    hb["halted_at"] = datetime.now(timezone.utc).isoformat() if halted else None
    hb["last_run"] = datetime.now(timezone.utc).isoformat()
    hb["last_routine"] = "Kill Switch"
    hb["status"] = "halted" if halted else hb.get("status", "ok")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(hb, fh, indent=2)
    except OSError as exc:
        _log(f"cannot write heartbeat.json: {exc}")
    return {"halted": halted, "reason": reason}


# --- CLI -------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading-bot emergency controls.")
    parser.add_argument("action", choices=("status", "halt", "reset", "close-all"))
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps({"halted": is_halted()}, indent=2))
    elif args.action == "halt":
        print(json.dumps(_set_halted(True, "manual halt"), indent=2))
    elif args.action == "reset":
        print(json.dumps(reset(), indent=2))
    elif args.action == "close-all":
        result = emergency_close_all("manual close-all")
        print(json.dumps({"closed_orders": result}, indent=2))
