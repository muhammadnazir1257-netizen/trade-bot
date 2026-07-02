#!/usr/bin/env python3
"""Trader agent tools.

Places **limit-only** orders against the Alpaca API, enforces the hard risk
rules via :func:`validate_order` (called before every order), and reports
market status and open orders. Used by the Trading Session routine.

CLI:
    python scripts/trade.py status
    python scripts/trade.py orders
    python scripts/trade.py cancel
    python scripts/trade.py validate SYMBOL QTY SIDE LIMIT_PRICE
    python scripts/trade.py order    SYMBOL QTY SIDE LIMIT_PRICE

All structured output is JSON on stdout. Errors go to stderr; non-zero exit.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests
from dotenv import load_dotenv

# Put project root on sys.path so `import config` works when run standalone.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

load_dotenv()

TRADING_BASE = os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
HTTP_TIMEOUT = int(os.environ.get("ALPACA_HTTP_TIMEOUT", "30"))


def _log(message: str) -> None:
    """Write a diagnostic line to stderr (never stdout)."""
    print(f"[trade] {message}", file=sys.stderr)


def is_crypto(symbol: str) -> bool:
    """Crypto symbols are pair-formatted (e.g. ``BTC/USD``)."""
    return "/" in (symbol or "")


def _norm(symbol: str) -> str:
    """Normalize a symbol for matching positions: Alpaca returns crypto
    positions as ``BTCUSD`` but accepts orders as ``BTC/USD``."""
    return (symbol or "").upper().replace("/", "")


def is_market_open_for(symbol: str) -> bool:
    """Crypto trades 24/7; equities follow the exchange clock."""
    if is_crypto(symbol):
        return True
    return bool(get_market_status().get("is_open", False))


def _headers() -> dict[str, str]:
    """Build auth headers from environment variables."""
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set. Copy .env.example to .env."
        )
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "content-type": "application/json",
        "accept": "application/json",
    }


def _request(method: str, path: str, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> Any:
    """Perform an authenticated request against the trading API."""
    url = f"{TRADING_BASE}{path}"
    try:
        resp = requests.request(
            method, url, headers=_headers(), params=params,
            json=body, timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{method} {url} returned non-JSON body") from exc


def _load_watchlist() -> dict[str, Any]:
    """Load watchlist.json from the project root."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "watchlist.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        _log(f"could not load watchlist.json: {exc}")
        return {}


def _get_account() -> dict[str, Any]:
    """Internal: fetch account snapshot for validation (cash + equity)."""
    try:
        acct = _request("GET", "/v2/account")
        return {
            "cash": float(acct.get("cash", 0.0)),
            "equity": float(acct.get("equity", 0.0)),
        }
    except RuntimeError as exc:
        _log(f"_get_account() error: {exc}")
        return {"cash": 0.0, "equity": 0.0}


# --- Core trading functions ------------------------------------------------


def get_market_status() -> dict[str, Any]:
    """Return market clock info.

    Returns:
        ``{"is_open": bool, "next_open": str, "next_close": str}``. On error,
        ``is_open`` is ``False`` (fail-safe: do not trade if status unknown).
    """
    try:
        clock = _request("GET", "/v2/clock")
        return {
            "is_open": bool(clock.get("is_open", False)),
            "next_open": clock.get("next_open", ""),
            "next_close": clock.get("next_close", ""),
        }
    except RuntimeError as exc:
        _log(f"get_market_status() error: {exc}")
        return {"is_open": False, "next_open": "", "next_close": "", "error": str(exc)}


def get_open_orders() -> list[dict[str, Any]]:
    """Return all open/pending orders."""
    try:
        orders = _request("GET", "/v2/orders", params={"status": "open", "limit": 100})
        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "qty": o.get("qty"),
                "side": o.get("side"),
                "type": o.get("type"),
                "limit_price": o.get("limit_price"),
                "status": o.get("status"),
                "submitted_at": o.get("submitted_at"),
            }
            for o in orders
        ]
    except RuntimeError as exc:
        _log(f"get_open_orders() error: {exc}")
        return []


def cancel_all_orders() -> dict[str, Any]:
    """Cancel every open order. Returns a summary dict."""
    try:
        result = _request("DELETE", "/v2/orders")
        canceled = result if isinstance(result, list) else []
        return {"canceled": len(canceled), "detail": canceled}
    except RuntimeError as exc:
        _log(f"cancel_all_orders() error: {exc}")
        return {"canceled": 0, "error": str(exc)}


def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel one open order by id. Returns ``{"canceled": bool, ...}``."""
    if not order_id:
        return {"canceled": False, "error": "no order id"}
    try:
        _request("DELETE", f"/v2/orders/{order_id}")
        return {"canceled": True, "order_id": order_id}
    except RuntimeError as exc:
        _log(f"cancel_order({order_id}) error: {exc}")
        return {"canceled": False, "order_id": order_id, "error": str(exc)}


def validate_order(
    symbol: str,
    qty: float,
    side: str,
    current_price: float,
    account_value: float,
    current_positions: list[dict[str, Any]],
    watchlist: dict[str, Any],
) -> tuple[bool, str]:
    """Pre-flight risk check enforcing every hard rule in CLAUDE.md.

    Args:
        symbol: Ticker.
        qty: Share quantity (> 0).
        side: ``"buy"`` or ``"sell"``.
        current_price: Reference price used for the limit/notional.
        account_value: Total equity.
        current_positions: Output of research.get_positions().
        watchlist: Parsed watchlist.json (policy thresholds).

    Returns:
        ``(ok, reason)``. ``ok`` is False if any rule is violated; ``reason``
        explains the first failing rule (or confirms all checks passed).
    """
    side = (side or "").lower()
    symbol = (symbol or "").upper()

    # Basic sanity
    if side not in ("buy", "sell"):
        return False, f"Invalid side '{side}' (must be buy or sell)."
    if qty is None or qty <= 0:
        return False, f"Invalid qty {qty} (must be > 0)."
    if current_price is None or current_price <= 0:
        return False, f"Invalid price {current_price} (must be > 0)."
    if account_value is None or account_value <= 0:
        return False, "Account value unavailable or zero; cannot size order safely."

    max_single_pct = watchlist.get("max_single_position_pct", 5)
    cash_reserve_pct = watchlist.get("cash_reserve_pct", 20)
    entries = {e["symbol"].upper(): e for e in watchlist.get("watchlist", [])}
    symbol_cap_pct = entries.get(symbol, {}).get("max_allocation_pct", max_single_pct)
    effective_cap_pct = min(max_single_pct, symbol_cap_pct)

    # Market must be open (crypto is always open)
    if not is_market_open_for(symbol):
        status = get_market_status()
        return False, "Market is closed; no orders permitted (next open %s)." % status.get("next_open", "?")

    notional = qty * current_price
    held = next((p for p in current_positions if _norm(p.get("symbol", "")) == _norm(symbol)), None)
    # Alpaca reports short positions with negative qty / market_value.
    held_qty = float(held["qty"]) if held else 0.0          # signed: + long, - short
    held_mv = float(held["market_value"]) if held else 0.0  # signed
    held_abs_mv = abs(held_mv)

    cap_value = (effective_cap_pct / 100.0) * account_value
    max_gross = _shorting_cfg("MAX_GROSS_EXPOSURE_PCT", 1.5) * account_value
    gross_now = sum(abs(float(p.get("market_value", 0.0))) for p in current_positions)
    # Gross exposure if this order's symbol exposure is replaced by the projection
    gross_excl_symbol = gross_now - held_abs_mv

    if side == "buy":
        if held_qty < 0:
            # Buy-to-cover an existing short
            if qty <= abs(held_qty) + 1e-9:
                return True, (
                    f"OK: BUY-to-cover {qty} {symbol} @ ~${current_price:.2f} "
                    f"(reducing short of {abs(held_qty):g})."
                )
            return False, (
                f"BUY {qty} exceeds short {abs(held_qty):g} {symbol}: would flip to long. "
                f"Cover the short fully in one order, then open a long separately."
            )
        # Opening / adding a long
        projected_long_mv = (held_mv if held_qty > 0 else 0.0) + notional
        if projected_long_mv > cap_value + 1e-6:
            return False, (
                f"Position cap breach: long {symbol} would be ${projected_long_mv:,.2f} "
                f"({projected_long_mv / account_value * 100:.2f}% of equity), "
                f"cap {effective_cap_pct}% (${cap_value:,.2f})."
            )
        if gross_excl_symbol + projected_long_mv > max_gross + 1e-6:
            return False, (
                f"Gross-exposure breach: total |exposure| would be "
                f"${gross_excl_symbol + projected_long_mv:,.2f} "
                f"(> {_shorting_cfg('MAX_GROSS_EXPOSURE_PCT',1.5)*100:.0f}% cap ${max_gross:,.2f})."
            )
        cash = _get_account().get("cash", 0.0)
        cash_after = cash - notional
        min_cash = (cash_reserve_pct / 100.0) * account_value
        if cash_after < min_cash - 1e-6:
            return False, (
                f"Cash-reserve breach: cash would fall to ${cash_after:,.2f}, "
                f"below the {cash_reserve_pct}% reserve (${min_cash:,.2f})."
            )
        return True, (
            f"OK: BUY {qty} {symbol} @ ~${current_price:.2f} "
            f"(notional ${notional:,.2f}, {notional / account_value * 100:.2f}% of equity, "
            f"within {effective_cap_pct}% cap; reserve + gross OK)."
        )

    # side == "sell"
    if held_qty > 0:
        # Sell-to-close / trim an existing long
        if qty <= held_qty + 1e-9:
            return True, (
                f"OK: SELL {qty} {symbol} @ ~${current_price:.2f} "
                f"(closing/trimming long of {held_qty:g})."
            )
        return False, (
            f"SELL {qty} exceeds long {held_qty:g} {symbol}: would flip to short. "
            f"Close the long fully in one order, then open a short separately."
        )

    # Opening / adding a short (held_qty <= 0)
    if is_crypto(symbol):
        return False, f"Cannot short {symbol}: Alpaca does not support crypto shorting."
    if not _shorting_cfg("SHORTING_ENABLED", True):
        return False, f"Shorting disabled by config; cannot open short {symbol}."
    projected_short_mv = held_abs_mv + notional   # held_abs_mv is the existing short's |mv|
    if projected_short_mv > cap_value + 1e-6:
        return False, (
            f"Position cap breach: short {symbol} would be ${projected_short_mv:,.2f} "
            f"({projected_short_mv / account_value * 100:.2f}% of equity), "
            f"cap {effective_cap_pct}% (${cap_value:,.2f})."
        )
    if gross_excl_symbol + projected_short_mv > max_gross + 1e-6:
        return False, (
            f"Gross-exposure breach: total |exposure| would be "
            f"${gross_excl_symbol + projected_short_mv:,.2f} "
            f"(> {_shorting_cfg('MAX_GROSS_EXPOSURE_PCT',1.5)*100:.0f}% cap ${max_gross:,.2f})."
        )
    return True, (
        f"OK: SHORT {qty} {symbol} @ ~${current_price:.2f} "
        f"(notional ${notional:,.2f}, {notional / account_value * 100:.2f}% of equity, "
        f"within {effective_cap_pct}% cap; gross OK)."
    )


def _shorting_cfg(name: str, default):
    """Read a config value without a hard import dependency (keeps trade.py
    runnable standalone if config.py is absent)."""
    try:
        import config  # type: ignore
        return getattr(config, name, default)
    except Exception:
        return default


def place_order(symbol: str, qty: float, side: str, limit_price: float) -> dict[str, Any]:
    """Place a **limit** order. Market orders are forbidden.

    Args:
        symbol: Ticker.
        qty: Share quantity.
        side: ``"buy"`` or ``"sell"``.
        limit_price: Limit price. ``None`` raises immediately.

    Returns:
        The created order dict from Alpaca.

    Raises:
        ValueError: if ``limit_price`` is None (market orders are forbidden).
        RuntimeError: on API failure.
    """
    if limit_price is None:
        raise ValueError(
            "limit_price is required — market orders are forbidden by policy."
        )
    crypto = is_crypto(symbol)
    # Crypto requires gtc/ioc (not "day") and allows fractional qty; equities
    # use "day" and whole shares.
    tif = _shorting_cfg("CRYPTO_TIME_IN_FORCE", "gtc") if crypto else "day"
    if crypto and _shorting_cfg("ALLOW_FRACTIONAL", True):
        # FLOOR to 8 decimals (Alpaca's precision). Rounding up can request
        # more than the actual position balance and trigger "insufficient
        # balance" on closes. Floor guarantees we ask for <= what we hold.
        import math
        floored = math.floor(float(qty) * 1e8) / 1e8
        if floored <= 0:
            raise ValueError(
                f"crypto qty {qty} floors to 0 at 8-decimal precision (dust)."
            )
        qty_str = f"{floored:.8f}".rstrip("0").rstrip(".")
    else:
        qty_str = str(int(qty)) if float(qty).is_integer() else str(qty)
    # Crypto limit prices can need more precision than 2 dp for low-priced coins
    price_str = (f"{float(limit_price):.2f}" if float(limit_price) >= 1 or not crypto
                 else f"{float(limit_price):.6f}")
    body = {
        "symbol": symbol.upper(),
        "qty": qty_str,
        "side": (side or "").lower(),
        "type": "limit",
        "time_in_force": tif,
        "limit_price": price_str,
    }
    order = _request("POST", "/v2/orders", body=body)
    return {
        "id": order.get("id"),
        "symbol": order.get("symbol"),
        "qty": order.get("qty"),
        "side": order.get("side"),
        "type": order.get("type"),
        "limit_price": order.get("limit_price"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at"),
    }


# --- CLI -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if not argv:
        _log("usage: trade.py [status|order|cancel|validate|orders] ...")
        return 2

    command = argv[0]
    try:
        if command == "status":
            print(json.dumps(get_market_status(), indent=2))
        elif command == "orders":
            print(json.dumps(get_open_orders(), indent=2))
        elif command == "cancel":
            print(json.dumps(cancel_all_orders(), indent=2))
        elif command in ("validate", "order"):
            if len(argv) < 5:
                _log(f"usage: trade.py {command} SYMBOL QTY SIDE LIMIT_PRICE")
                return 2
            symbol, qty, side, limit_price = argv[1], float(argv[2]), argv[3], float(argv[4])
            watchlist = _load_watchlist()
            # research.get_positions equivalent (avoid cross-import; reuse trading API)
            try:
                positions_raw = _request("GET", "/v2/positions")
            except RuntimeError:
                positions_raw = []
            positions = [
                {
                    "symbol": p.get("symbol"),
                    "qty": float(p.get("qty", 0.0)),
                    "market_value": float(p.get("market_value", 0.0)),
                }
                for p in positions_raw
            ]
            account_value = _get_account().get("equity", 0.0)
            ok, reason = validate_order(
                symbol, qty, side, limit_price, account_value, positions, watchlist
            )
            if command == "validate":
                print(json.dumps({"ok": ok, "reason": reason}, indent=2))
                return 0 if ok else 3
            # command == "order": validate, then place only if ok
            if not ok:
                print(json.dumps({"placed": False, "reason": reason}, indent=2))
                return 3
            result = place_order(symbol, qty, side, limit_price)
            print(json.dumps({"placed": True, "validation": reason, "order": result}, indent=2))
        else:
            _log(f"unknown command: {command}")
            return 2
    except ValueError as exc:
        _log(f"rejected: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        _log(f"fatal: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
