#!/usr/bin/env python3
"""Risk Reviewer agent tools.

Reads the day's journal, extracts the Trades Executed table, and independently
scores each trade against the hard risk rules. Risky trades are flagged in the
journal with ``⚠️ RISK FLAG:`` and ``heartbeat.json`` is updated so
``review_required`` becomes ``true``. The reviewer never trades or cancels.

CLI:
    python scripts/review.py review [YYYY-MM-DD]
    python scripts/review.py heartbeat
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo/tzdata unavailable
    _ET = timezone.utc

load_dotenv()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DIR = os.path.join(ROOT, "journal")
HEARTBEAT_PATH = os.path.join(ROOT, "heartbeat.json")
TRADING_BASE = os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
HTTP_TIMEOUT = int(os.environ.get("ALPACA_HTTP_TIMEOUT", "30"))


def _log(message: str) -> None:
    """Write a diagnostic line to stderr (never stdout)."""
    print(f"[review] {message}", file=sys.stderr)


def _today_et() -> str:
    """Return today's date (ET) as YYYY-MM-DD."""
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _headers() -> dict[str, str]:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set.")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "accept": "application/json"}


def _request(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{TRADING_BASE}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def _load_watchlist() -> dict[str, Any]:
    try:
        with open(os.path.join(ROOT, "watchlist.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        _log(f"could not load watchlist.json: {exc}")
        return {}


def _get_account() -> dict[str, Any]:
    try:
        a = _request("/v2/account")
        return {"cash": float(a.get("cash", 0.0)), "equity": float(a.get("equity", 0.0))}
    except RuntimeError as exc:
        _log(f"_get_account() error: {exc}")
        return {"cash": 0.0, "equity": 0.0}


def _get_positions() -> list[dict[str, Any]]:
    try:
        raw = _request("/v2/positions")
        return [
            {
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0.0)),
                "market_value": float(p.get("market_value", 0.0)),
                "unrealized_plpc": float(p.get("unrealized_plpc", 0.0)),
            }
            for p in raw
        ]
    except RuntimeError as exc:
        _log(f"_get_positions() error: {exc}")
        return []


# --- Journal parsing -------------------------------------------------------


def load_journal(date_str: str) -> str:
    """Read the journal markdown for ``date_str`` (YYYY-MM-DD).

    Returns the file contents, or an empty string if the journal is missing.
    """
    path = os.path.join(JOURNAL_DIR, f"{date_str}.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        _log(f"load_journal({date_str}) error: {exc}")
        return ""


def extract_trades(journal_text: str) -> list[dict[str, Any]]:
    """Parse the ``## Trades Executed`` markdown table into trade dicts.

    Each returned dict has: time, symbol, action, qty, limit_price (float|None),
    limit_price_raw (str), reasoning. Header, separator, and template
    placeholder rows are skipped.
    """
    if not journal_text:
        return []

    # Isolate the Trades Executed section (up to the next ## heading).
    section_match = re.search(
        r"##\s*Trades Executed\s*(.*?)(?:\n##\s|\Z)", journal_text, re.DOTALL
    )
    if not section_match:
        return []
    section = section_match.group(1)

    trades: list[dict[str, Any]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        first = cells[0].lower()
        # Skip header row and the |---|---| separator row.
        if first in ("time", "") or set(cells[0]) <= {"-", ":", " "}:
            continue
        if "{{" in line:  # untouched template placeholder
            continue

        raw_price = cells[4].replace("$", "").replace(",", "").strip()
        try:
            limit_price = float(raw_price) if raw_price not in ("", "—", "-", "n/a", "N/A") else None
        except ValueError:
            limit_price = None

        try:
            qty = float(cells[3]) if cells[3] not in ("", "—", "-") else 0.0
        except ValueError:
            qty = 0.0

        trades.append(
            {
                "time": cells[0],
                "symbol": cells[1].upper(),
                "action": cells[2].upper(),
                "qty": qty,
                "limit_price": limit_price,
                "limit_price_raw": cells[4],
                "reasoning": cells[5],
            }
        )
    return trades


# --- Risk evaluation -------------------------------------------------------


def evaluate_trade_risk(
    trade: dict[str, Any],
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    watchlist: dict[str, Any],
) -> dict[str, Any]:
    """Score a single trade against the hard rules.

    Returns ``{"passed": bool, "flags": [str]}``. HOLD rows always pass (they
    are decisions, not trades) but empty reasoning is still flagged.
    """
    flags: list[str] = []
    action = (trade.get("action") or "").upper()
    symbol = (trade.get("symbol") or "").upper()
    qty = float(trade.get("qty") or 0.0)
    limit_price = trade.get("limit_price")
    reasoning = (trade.get("reasoning") or "").strip()

    equity = account.get("equity", 0.0) or 0.0
    cash = account.get("cash", 0.0) or 0.0

    max_single_pct = watchlist.get("max_single_position_pct", 5)
    cash_reserve_pct = watchlist.get("cash_reserve_pct", 20)
    stop_loss_pct = watchlist.get("stop_loss_pct", 8)
    entries = {e["symbol"].upper(): e for e in watchlist.get("watchlist", [])}
    symbol_cap_pct = entries.get(symbol, {}).get("max_allocation_pct", max_single_pct)
    effective_cap_pct = min(max_single_pct, symbol_cap_pct)

    # Reasoning must always be present and meaningful.
    if len(reasoning) < 8:
        flags.append(f"{symbol}: missing or trivial reasoning ('{reasoning}').")

    if action == "HOLD":
        return {"passed": not flags, "flags": flags}

    if action not in ("BUY", "SELL"):
        flags.append(f"{symbol}: unrecognized action '{action}'.")
        return {"passed": False, "flags": flags}

    # Rule 2: limit orders only.
    if limit_price is None or limit_price <= 0:
        flags.append(
            f"{symbol}: not a valid limit order (limit price "
            f"'{trade.get('limit_price_raw')}') — market orders are forbidden."
        )

    notional = qty * (limit_price or 0.0)

    if action == "BUY" and equity > 0:
        held = next((p for p in positions if p["symbol"].upper() == symbol), None)
        held_mv = held["market_value"] if held else 0.0
        projected = held_mv + notional
        cap_value = (effective_cap_pct / 100.0) * equity
        if projected > cap_value + 1e-6:
            flags.append(
                f"{symbol}: position-cap breach — projected ${projected:,.2f} "
                f"({projected / equity * 100:.2f}% of equity) exceeds "
                f"{effective_cap_pct}% cap (${cap_value:,.2f})."
            )
        min_cash = (cash_reserve_pct / 100.0) * equity
        if (cash - notional) < min_cash - 1e-6:
            flags.append(
                f"{symbol}: cash-reserve breach — buying ${notional:,.2f} leaves "
                f"${cash - notional:,.2f}, below the {cash_reserve_pct}% reserve "
                f"(${min_cash:,.2f})."
            )

    return {"passed": not flags, "flags": flags}


def scan_stop_loss(positions: list[dict[str, Any]], watchlist: dict[str, Any]) -> list[str]:
    """Flag open positions sitting at or below the stop-loss threshold."""
    stop_loss_pct = watchlist.get("stop_loss_pct", 8)
    out = []
    for p in positions:
        plpc = p.get("unrealized_plpc", 0.0) * 100
        if plpc <= -stop_loss_pct:
            out.append(
                f"{p['symbol']}: open position down {plpc:.2f}% — at/below the "
                f"-{stop_loss_pct}% stop loss and was not closed today."
            )
    return out


# --- Journal + heartbeat writers ------------------------------------------


def flag_journal_entry(date_str: str, trade_symbol: str, flag_message: str) -> bool:
    """Append a ``⚠️ RISK FLAG:`` line under the journal's Risk Review section.

    Append-safe: writes under the ``## Risk Review`` heading, replacing the
    template placeholder if it is still present. Returns True on success.
    """
    path = os.path.join(JOURNAL_DIR, f"{date_str}.md")
    line = f"- ⚠️ RISK FLAG: {trade_symbol} — {flag_message}"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _log(f"flag_journal_entry: cannot read {path}: {exc}")
        return False

    if "{{RISK_REVIEW_NOTES}}" in text:
        text = text.replace("{{RISK_REVIEW_NOTES}}", line)
    elif "## Risk Review" in text:
        text = re.sub(
            r"(##\s*Risk Review\s*\n)", rf"\1{line}\n", text, count=1
        )
    else:
        text = text.rstrip() + f"\n\n## Risk Review\n{line}\n"

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except OSError as exc:
        _log(f"flag_journal_entry: cannot write {path}: {exc}")
        return False


def write_risk_review_summary(date_str: str, lines: list[str]) -> None:
    """Replace the Risk Review placeholder with the full set of findings."""
    path = os.path.join(JOURNAL_DIR, f"{date_str}.md")
    body = "\n".join(lines) if lines else "No risk flags. All trades within policy."
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _log(f"write_risk_review_summary: cannot read {path}: {exc}")
        return
    if "{{RISK_REVIEW_NOTES}}" in text:
        text = text.replace("{{RISK_REVIEW_NOTES}}", body)
    elif "## Risk Review" in text:
        text = re.sub(r"(##\s*Risk Review\s*\n).*?(?=\n##\s|\Z)",
                      rf"\1{body}\n", text, count=1, flags=re.DOTALL)
    else:
        text = text.rstrip() + f"\n\n## Risk Review\n{body}\n"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        _log(f"write_risk_review_summary: cannot write {path}: {exc}")


def update_heartbeat(status: str, flags: list[str]) -> dict[str, Any]:
    """Write system state to heartbeat.json. Always called at routine end."""
    state = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "last_routine": "Risk Review",
        "status": status,
        "review_required": bool(flags),
        "flags": flags,
    }
    try:
        if os.path.exists(HEARTBEAT_PATH):
            with open(HEARTBEAT_PATH, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            existing.update(state)
            state = existing
    except (OSError, ValueError) as exc:
        _log(f"update_heartbeat: could not merge existing heartbeat: {exc}")
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        _log(f"update_heartbeat: cannot write heartbeat.json: {exc}")
    return state


# --- CLI -------------------------------------------------------------------


def run_review(date_str: str) -> dict[str, Any]:
    """Full review pass: parse trades, evaluate, flag, update heartbeat."""
    journal = load_journal(date_str)
    if not journal:
        _log(f"no journal for {date_str}; nothing to review.")
        update_heartbeat("ok", [])
        return {"date": date_str, "trades": 0, "flags": [], "review_required": False}

    trades = extract_trades(journal)
    account = _get_account()
    positions = _get_positions()
    watchlist = _load_watchlist()

    all_flags: list[str] = []
    review_lines: list[str] = []
    for trade in trades:
        result = evaluate_trade_risk(trade, account, positions, watchlist)
        if result["flags"]:
            for msg in result["flags"]:
                all_flags.append(msg)
                review_lines.append(f"- ⚠️ RISK FLAG: {msg}")
                flag_journal_entry(date_str, trade["symbol"], msg)
        else:
            review_lines.append(
                f"- ✅ {trade['symbol']} {trade['action']}: within policy."
            )

    for sl in scan_stop_loss(positions, watchlist):
        all_flags.append(sl)
        review_lines.append(f"- ⚠️ RISK FLAG: {sl}")

    if not trades:
        review_lines.append("- No trades recorded today; nothing to evaluate.")

    write_risk_review_summary(date_str, review_lines)
    status = "review" if all_flags else "ok"
    update_heartbeat(status, all_flags)
    return {
        "date": date_str,
        "trades": len(trades),
        "flags": all_flags,
        "review_required": bool(all_flags),
    }


def main(argv: list[str]) -> int:
    if not argv:
        _log("usage: review.py [review|heartbeat] [YYYY-MM-DD]")
        return 2
    command = argv[0]
    try:
        if command == "review":
            date_str = argv[1] if len(argv) > 1 else _today_et()
            print(json.dumps(run_review(date_str), indent=2))
        elif command == "heartbeat":
            try:
                with open(HEARTBEAT_PATH, "r", encoding="utf-8") as fh:
                    print(json.dumps(json.load(fh), indent=2))
            except (OSError, ValueError) as exc:
                _log(f"cannot read heartbeat.json: {exc}")
                return 1
        else:
            _log(f"unknown command: {command}")
            return 2
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        _log(f"fatal: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
