#!/usr/bin/env python3
"""End-of-day report for the local intraday system.

Reads today's intraday_log JSONL trace + the per-day state file, pulls the
final account snapshot from Alpaca, computes the day's P&L, writes a
journal/<DATE>.md summary, and emails the digest via SendGrid (reusing
notify.send_email). Designed to be run by Windows Task Scheduler at ~16:15 ET.

CLI:
    python scripts/eod_report.py
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

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

import config            # noqa: E402
import research          # type: ignore  # noqa: E402
import notify            # type: ignore  # noqa: E402
import analytics         # type: ignore  # noqa: E402


def _log(msg: str) -> None:
    print(f"[eod_report] {msg}", file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return rows


def build_journal(date_str: str) -> str:
    """Construct journal/<date>.md from the intraday log + final account."""
    log_path = os.path.join(_ROOT, config.INTRADAY_LOG_DIR, f"{date_str}.jsonl")
    state_path = os.path.join(_ROOT, config.INTRADAY_LOG_DIR, f"state-{date_str}.json")
    rows = _read_jsonl(log_path)

    account = research.get_account()
    positions = research.get_positions()
    equity = float(account.get("equity", 0.0))

    start_equity = None
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            start_equity = float(json.load(fh).get("start_equity") or 0.0)
    except (OSError, ValueError):
        start_equity = None
    if not start_equity and rows:
        start_equity = float(rows[0].get("portfolio_value", equity) or equity)
    start_equity = start_equity or equity
    daily_pnl = equity - start_equity
    daily_pnl_pct = (daily_pnl / start_equity * 100) if start_equity else 0.0

    # Collect actions and signals across the day. DUST_HELD and CLOSE_PENDING
    # are bookkeeping states, not orders — hundreds of them per day were
    # drowning the trades table in noise.
    _NOISE_ACTIONS = ("NONE", "", "DUST_HELD", "CLOSE_PENDING")
    orders_placed = []
    dust_ticks = 0
    regime_seen = "unknown"
    composite_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for r in rows:
        regime_seen = r.get("regime", regime_seen)
        for s in r.get("signals", []):
            cs = s.get("composite_signal", "HOLD")
            composite_counts[cs] = composite_counts.get(cs, 0) + 1
            act = s.get("action_taken", "NONE")
            if act == "DUST_HELD":
                dust_ticks += 1
                continue
            if act not in _NOISE_ACTIONS and not act.startswith("REJECTED"):
                orders_placed.append({
                    "time": r.get("timestamp", "")[11:19],
                    "symbol": s.get("symbol"),
                    "action": act,
                    "signal": cs,
                    "confidence": s.get("composite_confidence"),
                    "order_id": s.get("order_id"),
                })

    # Dust crypto leftovers (unsellable sub-1e-6 fractions worth < $0.01)
    # are summarized in one clause instead of scaring the report with
    # phantom -20% "positions".
    real_positions = [p for p in positions if abs(float(p.get("qty", 0))) >= 1e-6]
    dust_positions = [p for p in positions if abs(float(p.get("qty", 0))) < 1e-6]
    pos_list = ", ".join(
        f"{p['symbol']} {'SHORT' if p['qty'] < 0 else 'LONG'} {abs(p['qty']):g} "
        f"@ ${p['avg_entry_price']:.2f} ({p['unrealized_plpc']*100:+.2f}%)"
        for p in real_positions
    ) or "none"
    if dust_positions:
        pos_list += (f" (+{len(dust_positions)} dust crypto remnant(s) "
                     f"< $0.01 total, unsellable)")

    # Accuracy tracker summary (per-strategy win rates from the close-loop)
    acc_lines = []
    try:
        acc_path = os.path.join(_ROOT, config.ACCURACY_TRACKER_PATH)
        with open(acc_path, "r", encoding="utf-8") as fh:
            acc = json.load(fh)
        for strat, rec in sorted(acc.items()):
            w, l = int(rec.get("wins", 0)), int(rec.get("losses", 0))
            tot = w + l
            wr = f"{w/tot:.0%}" if tot else "n/a"
            acc_lines.append(f"- {strat}: {w}W/{l}L ({wr}), cum pnl {rec.get('pnl_sum',0):+.3f}")
    except (OSError, ValueError):
        pass

    lines = [
        f"# Trade Journal — {date_str}",
        "",
        "## Portfolio Status",
        f"- Cash: ${account.get('cash', 0.0):,.2f}",
        f"- Total Equity: ${equity:,.2f}",
        f"- Open Positions: {pos_list}",
        f"- Day P&L: ${daily_pnl:,.2f} ({daily_pnl_pct:+.2f}%)",
        f"- Market regime: {regime_seen}",
        f"- Iterations logged: {len(rows)}",
        "",
        "## Trades Executed",
        "| Time | Symbol | Action | Signal | Confidence | Order ID |",
        "|------|--------|--------|--------|------------|----------|",
    ]
    if orders_placed:
        for o in orders_placed:
            conf = f"{o['confidence']:.2f}" if isinstance(o.get("confidence"), (int, float)) else "—"
            lines.append(
                f"| {o['time']} ET | {o['symbol']} | {o['action']} | {o['signal']} | "
                f"{conf} | {o.get('order_id') or '—'} |"
            )
    else:
        lines.append("| — | ALL | HOLD | — | — | No orders placed today (no consensus signals crossed threshold). |")

    # Lifetime performance block (equity curve, Sharpe, expectancy) — computed
    # from the same logs, fails soft if analytics can't run.
    perf_lines: list[str] = []
    try:
        perf_lines = analytics.summary_lines()
    except Exception as exc:  # noqa: BLE001
        _log(f"analytics summary failed: {exc}")

    lines += [
        "",
        "## Signal Summary",
        f"- Composite signals across the day: BUY {composite_counts.get('BUY',0)}, "
        f"SELL {composite_counts.get('SELL',0)}, HOLD {composite_counts.get('HOLD',0)}",
        "",
        "## Performance (lifetime)",
        *(perf_lines or ["- analytics unavailable"]),
        "",
        "## Strategy Accuracy (lifetime, from close-loop)",
        *(acc_lines or ["- No closed trades recorded yet."]),
        "",
        "## End-of-Day Reflection",
        (f"Local intraday engine ran {len(rows)} iterations in the {regime_seen} regime. "
         f"Day P&L ${daily_pnl:,.2f} ({daily_pnl_pct:+.2f}%). "
         f"{len(orders_placed)} order action(s) taken. "
         f"Open positions at close: {pos_list}."),
        f"Tomorrow watch: monitor open positions against 8% stop; regime was {regime_seen}.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    date_str = _today_et()
    journal_dir = os.path.join(_ROOT, config.JOURNAL_DIR)
    os.makedirs(journal_dir, exist_ok=True)
    journal_path = os.path.join(journal_dir, f"{date_str}.md")

    body_md = build_journal(date_str)
    try:
        with open(journal_path, "w", encoding="utf-8") as fh:
            fh.write(body_md)
        _log(f"wrote {journal_path}")
    except OSError as exc:
        _log(f"could not write journal: {exc}")

    # Update heartbeat
    hb_path = os.path.join(_ROOT, config.HEARTBEAT_PATH)
    try:
        with open(hb_path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        hb = {}
    hb.update({
        "last_run": datetime.now(timezone.utc).isoformat(),
        "last_routine": "EOD Report (local)",
        "status": "ok",
    })
    try:
        with open(hb_path, "w", encoding="utf-8") as fh:
            json.dump(hb, fh, indent=2)
    except OSError:
        pass

    # Email the digest (reuses notify's SendGrid path + dry-run fallback)
    subject, body = notify.build_digest(journal_path)
    notify.send_email(subject, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
