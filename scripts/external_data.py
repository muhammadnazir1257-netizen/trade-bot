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


# --- StockTwits social sentiment (free, no key) -----------------------------


def _stocktwits_symbol(symbol: str) -> str:
    """Map our symbols to StockTwits format (crypto: BTC/USD -> BTC.X)."""
    if "/" in symbol:
        return symbol.split("/")[0].upper() + ".X"
    return symbol.upper()


def stocktwits_sentiment(symbol: str) -> dict[str, Any]:
    """Social sentiment from the StockTwits public stream.

    Returns a normalized point-in-time signal:
    ``{"value": -1..+1, "confidence": 0..1, "n_labeled": int, "asof": iso}``.
    value = (bullish - bearish) / labeled messages; confidence scales with
    how many of the last ~30 messages carried an explicit label. Fails open
    (zero signal) on any error. Cached 1h.
    """
    zero = {"value": 0.0, "confidence": 0.0, "n_labeled": 0,
            "asof": datetime.now(timezone.utc).isoformat()}
    cached = _cache_read(symbol, "stocktwits", ttl_hours=1.0)
    if isinstance(cached, dict) and "value" in cached:
        return cached
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/"
            f"{_stocktwits_symbol(symbol)}.json",
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "trade-bot/1.0"},
        )
        if r.status_code >= 400:
            _log(f"stocktwits {symbol} HTTP {r.status_code}")
            _cache_write(symbol, "stocktwits", zero)
            return zero
        msgs = (r.json() or {}).get("messages", []) or []
    except (requests.RequestException, ValueError) as exc:
        _log(f"stocktwits {symbol} failed: {exc}")
        return zero

    bull = bear = 0
    for m in msgs:
        basic = (((m.get("entities") or {}).get("sentiment") or {}) or {}).get("basic", "")
        if basic == "Bullish":
            bull += 1
        elif basic == "Bearish":
            bear += 1
    labeled = bull + bear
    out = dict(zero)
    if labeled >= 5:   # below this the sample is pure noise
        out["value"] = float(max(-1.0, min(1.0, (bull - bear) / labeled)))
        out["confidence"] = float(min(1.0, labeled / 20.0))
        out["n_labeled"] = labeled
    _cache_write(symbol, "stocktwits", out)
    return out


# --- Alpaca News coverage (free with existing keys) --------------------------


def alpaca_news_count(symbol: str, hours: int = 24) -> int:
    """Number of Alpaca News articles for the symbol in the last N hours.
    Used as a coverage/attention input to sentiment confidence — Alpaca News
    has no sentiment scores, so it never sets direction. Cached 1h."""
    if "/" in symbol:
        return 0
    cached = _cache_read(symbol, "alpacanews", ttl_hours=1.0)
    if cached is not None:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass
    try:
        import research  # local, reuses auth + retry
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        data = research._request(  # noqa: SLF001
            research.DATA_BASE, "/v1beta1/news",
            params={"symbols": symbol.upper(), "start": start, "limit": 50},
        )
        n = len(data.get("news", []) or [])
    except Exception as exc:  # noqa: BLE001
        _log(f"alpaca news {symbol} failed: {exc}")
        return 0
    _cache_write(symbol, "alpacanews", n)
    return n


# --- Combined normalized sentiment -------------------------------------------


def combined_sentiment(symbol: str) -> dict[str, Any]:
    """Blend Alpha Vantage news sentiment and StockTwits social sentiment
    into one point-in-time signal: ``{"value", "confidence", "asof",
    "components"}``. Each source contributes weighted by its own confidence;
    Alpaca News coverage nudges confidence up when a symbol is in the news.
    Fails open to a zero signal."""
    av_value = news_sentiment(symbol)                     # [-1, +1], 0 if thin
    av_conf = 0.6 if av_value != 0.0 else 0.0             # AV gates on article count internally
    st = stocktwits_sentiment(symbol)
    st_value, st_conf = float(st.get("value", 0.0)), float(st.get("confidence", 0.0))

    total_conf = av_conf + st_conf
    value = ((av_value * av_conf + st_value * st_conf) / total_conf) if total_conf > 0 else 0.0

    coverage = alpaca_news_count(symbol)
    confidence = min(1.0, (total_conf / 1.6) + min(coverage, 10) * 0.02)

    return {
        "value": float(max(-1.0, min(1.0, value))),
        "confidence": float(confidence),
        "asof": datetime.now(timezone.utc).isoformat(),
        "components": {"alphavantage": av_value, "stocktwits": st_value,
                       "news_coverage_24h": coverage},
    }


# --- Macro event calendar (scheduled releases, known in advance) --------------

# Point-in-time safe: these are the Fed's and BLS's PUBLISHED schedules for
# 2026 H2 (announced months in advance — using them is not lookahead).
# Dates are the release/decision day (ET). Verify against the official
# calendars when extending into 2027.
MACRO_EVENTS = [
    {"date": "2026-07-14", "event": "CPI release"},
    {"date": "2026-07-29", "event": "FOMC rate decision"},
    {"date": "2026-08-12", "event": "CPI release"},
    {"date": "2026-09-11", "event": "CPI release"},
    {"date": "2026-09-16", "event": "FOMC rate decision"},
    {"date": "2026-10-13", "event": "CPI release"},
    {"date": "2026-10-28", "event": "FOMC rate decision"},
    {"date": "2026-11-10", "event": "CPI release"},
    {"date": "2026-12-09", "event": "FOMC rate decision"},
    {"date": "2026-12-10", "event": "CPI release"},
]


def upcoming_macro_event(hours: int = 24) -> dict[str, Any] | None:
    """Return the next macro event within ``hours``, else None. Market-wide
    (not per-symbol): FOMC/CPI move everything."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    for ev in MACRO_EVENTS:
        try:
            # Treat the event as 14:00 UTC on release day (CPI 8:30 ET,
            # FOMC 14:00 ET — 14:00 UTC covers the pre-release window).
            ev_dt = datetime.fromisoformat(ev["date"] + "T14:00:00+00:00")
        except ValueError:
            continue
        if now <= ev_dt <= horizon:
            return {**ev, "at": ev_dt.isoformat()}
    return None


# --- CLI: quick inspection -------------------------------------------------


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Query external data providers.")
    parser.add_argument("action", choices=("earnings", "sentiment", "stocktwits",
                                           "combined", "macro"))
    parser.add_argument("symbol", nargs="?", default="SPY")
    parser.add_argument("--days", type=int, default=14, help="days_ahead for earnings")
    args = parser.parse_args()
    if args.action == "earnings":
        print(json.dumps(upcoming_earnings(args.symbol, days_ahead=args.days), indent=2))
    elif args.action == "sentiment":
        print(json.dumps({"symbol": args.symbol, "sentiment": news_sentiment(args.symbol)}, indent=2))
    elif args.action == "stocktwits":
        print(json.dumps(stocktwits_sentiment(args.symbol), indent=2))
    elif args.action == "combined":
        print(json.dumps(combined_sentiment(args.symbol), indent=2))
    elif args.action == "macro":
        print(json.dumps({"next_within_72h": upcoming_macro_event(72)}, indent=2))
