"""External data providers — Finnhub (earnings calendar + news) and Alpha
Vantage (news + sentiment). Both are file-cached per symbol with TTLs so we
stay comfortably inside the free-tier rate limits (Finnhub 60/min, AV 5/min
and 500/day).

Public functions:
    has_upcoming_earnings(symbol, hours) -> bool
    upcoming_earnings(symbol, days_ahead=2) -> dict | None
    news_sentiment(symbol) -> float           # in [-1, +1]; 0.0 if unavailable

All functions fail OPEN (return safe defaults on any error) so a provider
outage never blocks trading — it just removes the extra edge for that tick.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

load_dotenv()

import config  # noqa: E402

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")
HTTP_TIMEOUT = int(os.environ.get("EXTERNAL_HTTP_TIMEOUT", "20"))


def _log(msg: str) -> None:
    print(f"[external_data] {msg}", file=sys.stderr)


def _cache_path(symbol: str, kind: str) -> str:
    safe = symbol.replace("/", "_").upper()
    d = os.path.join(_ROOT, config.EXTERNAL_CACHE_DIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe}_{kind}.json")


def _cache_read(symbol: str, kind: str, ttl_hours: float) -> Any:
    path = _cache_path(symbol, kind)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    ts = blob.get("ts")
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
        if age_h > ttl_hours:
            return None
    except (ValueError, TypeError):
        return None
    return blob.get("value")


def _cache_write(symbol: str, kind: str, value: Any) -> None:
    path = _cache_path(symbol, kind)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ts": datetime.now(timezone.utc).isoformat(), "value": value}, fh)
    except OSError as exc:
        _log(f"cache write failed for {symbol}/{kind}: {exc}")


# --- Finnhub: upcoming earnings -------------------------------------------


def upcoming_earnings(symbol: str, days_ahead: int = 2) -> dict | None:
    """Return the nearest earnings event for ``symbol`` within ``days_ahead``,
    or None. Crypto and non-equity always return None.
    """
    if "/" in symbol or not FINNHUB_KEY:
        return None
    # Cache holds the next 14-day window per symbol (cheap to query infrequently)
    cached = _cache_read(symbol, "earnings", ttl_hours=config.EARNINGS_CACHE_TTL_HOURS)
    if cached is None:
        cached = _refresh_earnings(symbol)
    if not cached:
        return None
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    for item in cached:
        try:
            d = date.fromisoformat(item.get("date", ""))
        except (ValueError, TypeError):
            continue
        if today <= d <= cutoff:
            return item
    return None


def has_upcoming_earnings(symbol: str, hours: int = 48) -> bool:
    """Convenience wrapper used by the entry gate."""
    days = max(1, int(hours // 24) + (1 if hours % 24 else 0))
    return upcoming_earnings(symbol, days_ahead=days) is not None


def _refresh_earnings(symbol: str) -> list[dict]:
    today = date.today()
    end = today + timedelta(days=14)
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": today.isoformat(), "to": end.isoformat(),
                    "symbol": symbol.upper(), "token": FINNHUB_KEY},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            _log(f"finnhub earnings {symbol} HTTP {r.status_code}")
            return []
        items = r.json().get("earningsCalendar", []) or []
    except requests.RequestException as exc:
        _log(f"finnhub earnings {symbol} failed: {exc}")
        return []
    # Sort by date ascending — nearest first
    items.sort(key=lambda x: x.get("date", "9999-99-99"))
    _cache_write(symbol, "earnings", items)
    return items


# --- Alpha Vantage: news sentiment ----------------------------------------


def news_sentiment(symbol: str) -> float:
    """Relevance-weighted average of ticker_sentiment_score across recent
    articles. Returns a float in [-1, +1]; 0.0 if unavailable or below the
    minimum-articles threshold.
    """
    if "/" in symbol or not AV_KEY:
        return 0.0
    cached = _cache_read(symbol, "sentiment",
                         ttl_hours=config.SENTIMENT_CACHE_TTL_HOURS)
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass
    try:
        r = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "NEWS_SENTIMENT", "tickers": symbol.upper(),
                    "apikey": AV_KEY, "limit": 50},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            _log(f"AV sentiment {symbol} HTTP {r.status_code}")
            _cache_write(symbol, "sentiment", 0.0)
            return 0.0
        data = r.json()
    except requests.RequestException as exc:
        _log(f"AV sentiment {symbol} failed: {exc}")
        return 0.0

    # Free-tier rate-limit responses come back as a JSON body with a "Note" key.
    if "Note" in data or "Information" in data:
        _log(f"AV rate-limited for {symbol}: {data.get('Note') or data.get('Information')}")
        # Don't cache a rate-limit hit aggressively — short TTL
        _cache_write(symbol, "sentiment", 0.0)
        return 0.0

    feed = data.get("feed", []) or []
    weighted = []
    for art in feed:
        for ts in art.get("ticker_sentiment", []) or []:
            if str(ts.get("ticker", "")).upper() != symbol.upper():
                continue
            try:
                rel = float(ts.get("relevance_score", 0))
                score = float(ts.get("ticker_sentiment_score", 0))
            except (TypeError, ValueError):
                continue
            if rel > 0:
                weighted.append((score, rel))
    if len(weighted) < config.SENTIMENT_MIN_ARTICLES:
        _cache_write(symbol, "sentiment", 0.0)
        return 0.0
    total_w = sum(w for _, w in weighted)
    avg = sum(s * w for s, w in weighted) / total_w if total_w > 0 else 0.0
    avg = float(max(-1.0, min(1.0, avg)))
    _cache_write(symbol, "sentiment", avg)
    return avg


# --- CLI: quick inspection -------------------------------------------------


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Query external data providers.")
    parser.add_argument("action", choices=("earnings", "sentiment"))
    parser.add_argument("symbol")
    parser.add_argument("--days", type=int, default=14, help="days_ahead for earnings")
    args = parser.parse_args()
    if args.action == "earnings":
        print(json.dumps(upcoming_earnings(args.symbol, days_ahead=args.days), indent=2))
    elif args.action == "sentiment":
        print(json.dumps({"symbol": args.symbol, "sentiment": news_sentiment(args.symbol)}, indent=2))
