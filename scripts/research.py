#!/usr/bin/env python3
"""Researcher agent tools.

Pulls price history, moving averages, account state, open positions, and recent
news from the Alpaca API. Used by the Morning Research routine to build the
``## Market Research`` section of the daily journal.

CLI:
    python scripts/research.py bars      SYMBOL
    python scripts/research.py news      SYMBOL
    python scripts/research.py positions
    python scripts/research.py account
    python scripts/research.py risk

All output is JSON on stdout so the calling agent can parse it. Errors are
logged to stderr and the process exits non-zero.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---------------------------------------------------------

TRADING_BASE = os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_BASE = os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
CRYPTO_DATA_BASE = os.environ.get("CRYPTO_DATA_URL", "https://data.alpaca.markets").rstrip("/")
DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex")
HTTP_TIMEOUT = int(os.environ.get("ALPACA_HTTP_TIMEOUT", "30"))


def is_crypto(symbol: str) -> bool:
    """Crypto symbols are pair-formatted (e.g. ``BTC/USD``)."""
    return "/" in (symbol or "")


def _log(message: str) -> None:
    """Write a diagnostic line to stderr (never stdout)."""
    print(f"[research] {message}", file=sys.stderr)


def _headers() -> dict[str, str]:
    """Build auth headers from environment variables.

    Raises:
        RuntimeError: if API credentials are not configured.
    """
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set. Copy .env.example to .env."
        )
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
    }


_RETRY_STATUSES = (429, 500, 502, 503, 504)
_MAX_RETRIES = 3


def _request(base: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET against an Alpaca API base, with retry/backoff on transient errors.

    Retries connection failures, 429 rate limits (honoring Retry-After), and
    5xx responses. Hard 4xx errors surface immediately.

    Raises:
        RuntimeError: on unrecoverable failure or exhausted retries.
    """
    url = f"{base}{path}"
    last_err = ""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:  # network-level failure
            last_err = f"GET {url} failed: {exc}"
            if attempt < _MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_err) from exc
        if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
            try:
                delay = float(resp.headers.get("Retry-After", "") or 1.5 * (attempt + 1))
            except ValueError:
                delay = 1.5 * (attempt + 1)
            _log(f"HTTP {resp.status_code} on {path}; retrying in {delay:.1f}s")
            time.sleep(min(delay, 15.0))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"GET {url} returned non-JSON body") from exc
    raise RuntimeError(last_err or f"GET {url} exhausted retries")


# --- Core data functions ---------------------------------------------------


def get_bars(symbol: str, timeframe: str = "1Day", limit: int = 60) -> list[dict[str, Any]]:
    """Return up to ``limit`` price bars for ``symbol``.

    Args:
        symbol: Ticker, e.g. ``"SPY"``.
        timeframe: Alpaca timeframe string (default ``"1Day"``).
        limit: Maximum number of bars (default 60).

    Returns:
        List of bar dicts with keys t, o, h, l, c, v. Empty list on no data.
    """
    # Fetch the MOST RECENT `limit` bars with enough history depth:
    #   - `start` opens a lookback window wide enough to contain `limit` bars
    #   - `sort=desc` returns newest-first within that window (capped at limit)
    #   - reverse() restores chronological order
    # `start` + default ascending sort returns the OLDEST bars in the window
    # (day-stale intraday data); `sort=desc` with no `start` returns only the
    # current day (too little history). Both together is correct.
    days_back = _lookback_days(timeframe, limit)
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if is_crypto(symbol):
        # Crypto uses the v1beta3 endpoint; bars are keyed by symbol and there
        # is no feed/adjustment parameter. Markets are 24/7.
        try:
            data = _request(
                CRYPTO_DATA_BASE,
                "/v1beta3/crypto/us/bars",
                params={
                    "symbols": symbol.upper(),
                    "timeframe": timeframe,
                    "limit": limit,
                    "start": start,
                    "sort": "desc",
                },
            )
            bars = (data.get("bars") or {}).get(symbol.upper(), [])
            bars.reverse()
            return bars
        except RuntimeError as exc:
            _log(f"get_bars({symbol}) crypto error: {exc}")
            return []

    try:
        data = _request(
            DATA_BASE,
            f"/v2/stocks/{symbol.upper()}/bars",
            params={
                "timeframe": timeframe,
                "limit": limit,
                "start": start,
                "sort": "desc",
                "feed": DATA_FEED,
                "adjustment": "raw",
            },
        )
        bars = data.get("bars") or []
        bars.reverse()  # newest-first → chronological (oldest-first)
        return bars
    except RuntimeError as exc:
        _log(f"get_bars({symbol}) error: {exc}")
        return []


def _lookback_days(timeframe: str, limit: int) -> int:
    """Estimate calendar days needed to contain ``limit`` bars of ``timeframe``.

    Accounts for ~390 trading minutes/day and weekends (×1.7 buffer).
    """
    tf = timeframe.lower()
    if "day" in tf or "week" in tf or "month" in tf:
        return max(int(limit * 1.6), 7)
    import re
    m = re.match(r"(\d+)\s*(min|hour)", tf)
    mult = int(m.group(1)) if m else 1
    minutes_per_bar = mult * (60 if "hour" in tf else 1)
    trading_days_needed = (limit * minutes_per_bar) / 390.0
    return max(int(trading_days_needed * 1.7) + 2, 3)


def get_account() -> dict[str, Any]:
    """Return the trading account snapshot (cash, equity, buying power)."""
    try:
        acct = _request(TRADING_BASE, "/v2/account")
        return {
            "cash": float(acct.get("cash", 0.0)),
            "equity": float(acct.get("equity", 0.0)),
            "portfolio_value": float(acct.get("portfolio_value", acct.get("equity", 0.0))),
            "buying_power": float(acct.get("buying_power", 0.0)),
            "status": acct.get("status", "UNKNOWN"),
        }
    except RuntimeError as exc:
        _log(f"get_account() error: {exc}")
        return {"cash": 0.0, "equity": 0.0, "portfolio_value": 0.0, "buying_power": 0.0, "status": "ERROR"}


def get_positions() -> list[dict[str, Any]]:
    """Return all open positions with entry price and P&L."""
    try:
        raw = _request(TRADING_BASE, "/v2/positions")
        positions = []
        for p in raw:
            positions.append(
                {
                    "symbol": p.get("symbol"),
                    "qty": float(p.get("qty", 0.0)),
                    "avg_entry_price": float(p.get("avg_entry_price", 0.0)),
                    "current_price": float(p.get("current_price", 0.0)),
                    "market_value": float(p.get("market_value", 0.0)),
                    "unrealized_pl": float(p.get("unrealized_pl", 0.0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0.0)),
                }
            )
        return positions
    except RuntimeError as exc:
        _log(f"get_positions() error: {exc}")
        return []


def get_news(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return recent news items for ``symbol`` (most recent first)."""
    try:
        data = _request(
            DATA_BASE,
            "/v1beta1/news",
            params={"symbols": symbol.upper(), "limit": limit, "sort": "desc"},
        )
        items = []
        for n in data.get("news", []):
            items.append(
                {
                    "headline": n.get("headline", ""),
                    "summary": (n.get("summary", "") or "")[:280],
                    "source": n.get("source", ""),
                    "created_at": n.get("created_at", ""),
                    "url": n.get("url", ""),
                }
            )
        return items
    except RuntimeError as exc:
        _log(f"get_news({symbol}) error: {exc}")
        return []


def calculate_moving_averages(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute 20-day and 50-day simple moving averages from ``bars``.

    Args:
        bars: Output of :func:`get_bars` (chronological, oldest first).

    Returns:
        Dict with last_close, ma_20, ma_50, and a coarse trend label.
        MA values are ``None`` when there is insufficient history.
    """
    closes = [float(b["c"]) for b in bars if "c" in b]
    if not closes:
        return {"last_close": None, "ma_20": None, "ma_50": None, "trend": "unknown"}

    def _sma(values: list[float], window: int) -> float | None:
        if len(values) < window:
            return None
        return round(sum(values[-window:]) / window, 4)

    last_close = round(closes[-1], 4)
    ma_20 = _sma(closes, 20)
    ma_50 = _sma(closes, 50)

    trend = "unknown"
    if ma_20 is not None and ma_50 is not None:
        if last_close > ma_20 > ma_50:
            trend = "bullish (price > MA20 > MA50)"
        elif last_close < ma_20 < ma_50:
            trend = "bearish (price < MA20 < MA50)"
        elif ma_20 > ma_50:
            trend = "uptrend (MA20 > MA50)"
        else:
            trend = "downtrend (MA20 < MA50)"
    elif ma_20 is not None:
        trend = "above MA20" if last_close > ma_20 else "below MA20"

    return {"last_close": last_close, "ma_20": ma_20, "ma_50": ma_50, "trend": trend}


def summarize_position_risk(
    positions: list[dict[str, Any]], account: dict[str, Any]
) -> dict[str, Any]:
    """Return a risk summary for current holdings against the watchlist policy.

    Flags positions that are below the stop-loss threshold or that exceed the
    single-position cap, and reports the current cash-reserve ratio.
    """
    policy = _load_watchlist()
    stop_loss_pct = policy.get("stop_loss_pct", 8)
    max_single_pct = policy.get("max_single_position_pct", 5)
    cash_reserve_pct = policy.get("cash_reserve_pct", 20)

    equity = account.get("equity", 0.0) or 0.0
    cash = account.get("cash", 0.0) or 0.0
    cash_ratio = round((cash / equity) * 100, 2) if equity else 0.0

    details = []
    for p in positions:
        pct_of_equity = round((p["market_value"] / equity) * 100, 2) if equity else 0.0
        plpc = round(p["unrealized_plpc"] * 100, 2)
        flags = []
        if plpc <= -stop_loss_pct:
            flags.append(f"STOP-LOSS: down {plpc}% (limit -{stop_loss_pct}%)")
        if pct_of_equity > max_single_pct:
            flags.append(f"OVERSIZED: {pct_of_equity}% of equity (cap {max_single_pct}%)")
        details.append(
            {
                "symbol": p["symbol"],
                "pct_of_equity": pct_of_equity,
                "unrealized_plpc": plpc,
                "flags": flags,
            }
        )

    return {
        "equity": equity,
        "cash": cash,
        "cash_ratio_pct": cash_ratio,
        "cash_reserve_ok": cash_ratio >= cash_reserve_pct,
        "cash_reserve_target_pct": cash_reserve_pct,
        "positions": details,
        "any_flags": any(d["flags"] for d in details) or cash_ratio < cash_reserve_pct,
    }


def _load_watchlist() -> dict[str, Any]:
    """Load watchlist.json from the project root (best-effort)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "watchlist.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        _log(f"could not load watchlist.json: {exc}")
        return {}


# --- CLI -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if not argv:
        _log("usage: research.py [bars|news|positions|account|risk] [SYMBOL]")
        return 2

    command = argv[0]
    symbol = argv[1] if len(argv) > 1 else None

    try:
        if command == "bars":
            if not symbol:
                _log("bars requires a SYMBOL")
                return 2
            bars = get_bars(symbol)
            print(json.dumps({"symbol": symbol.upper(), "bars": bars,
                              "moving_averages": calculate_moving_averages(bars)}, indent=2))
        elif command == "news":
            if not symbol:
                _log("news requires a SYMBOL")
                return 2
            print(json.dumps({"symbol": symbol.upper(), "news": get_news(symbol)}, indent=2))
        elif command == "positions":
            print(json.dumps(get_positions(), indent=2))
        elif command == "account":
            print(json.dumps(get_account(), indent=2))
        elif command == "risk":
            print(json.dumps(summarize_position_risk(get_positions(), get_account()), indent=2))
        else:
            _log(f"unknown command: {command}")
            return 2
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        _log(f"fatal: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
