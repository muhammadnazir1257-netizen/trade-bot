#!/usr/bin/env python3
"""End-of-day digest emailer.

Reads today's journal and ``heartbeat.json``, then sends a plain-text summary
email via SendGrid. If ``review_required`` is true in the heartbeat, the
subject is prefixed with ``⚠️ ACTION NEEDED:``.

CLI:
    python scripts/notify.py                       # uses today's journal (ET)
    python scripts/notify.py journal/2026-05-19.md # explicit journal path

If SENDGRID_API_KEY is not set, the digest is printed to stdout instead of
sent (safe local dry-run) and the process exits 0.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# Journal bodies contain ✅ / ⚠️ emoji; Windows defaults stdout to cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # older Python without reconfigure
    pass

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

load_dotenv()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEARTBEAT_PATH = os.path.join(ROOT, "heartbeat.json")


def _log(message: str) -> None:
    """Write a diagnostic line to stderr (never stdout)."""
    print(f"[notify] {message}", file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        _log(f"could not read {path}: {exc}")
        return ""


def _read_heartbeat() -> dict:
    import json

    try:
        with open(HEARTBEAT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        _log(f"could not read heartbeat.json: {exc}")
        return {}


def build_digest(journal_path: str) -> tuple[str, str]:
    """Build the (subject, body) for the end-of-day email."""
    date_str = _today_et()
    if journal_path:
        base = os.path.basename(journal_path)
        if base.endswith(".md") and len(base) >= 13:
            date_str = base[:-3]

    journal = _read_text(journal_path) or "(journal not found)"
    hb = _read_heartbeat()
    review_required = bool(hb.get("review_required", False))
    flags = hb.get("flags", []) or []

    subject = f"Trading Agent Report — {date_str}"
    if review_required:
        subject = f"⚠️ ACTION NEEDED: {subject}"

    header_lines = [
        f"Trading Agent Report — {date_str}",
        "=" * 48,
        f"System status : {hb.get('status', 'unknown')}",
        f"Last routine  : {hb.get('last_routine', 'unknown')}",
        f"Last run (UTC): {hb.get('last_run', 'unknown')}",
        f"Review needed : {'YES' if review_required else 'no'}",
        "",
    ]
    if flags:
        header_lines.append("RISK FLAGS:")
        header_lines.extend(f"  - {f}" for f in flags)
        header_lines.append("")
    header_lines.append("-" * 48)
    header_lines.append("FULL JOURNAL")
    header_lines.append("-" * 48)

    body = "\n".join(header_lines) + "\n" + journal
    return subject, body


def send_email(subject: str, body: str) -> bool:
    """Send the digest via SendGrid. Returns True if sent.

    Falls back to printing the digest to stdout (dry-run) when
    SENDGRID_API_KEY is absent.
    """
    api_key = os.environ.get("SENDGRID_API_KEY")
    to_email = os.environ.get("NOTIFY_EMAIL")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", to_email)

    if not api_key:
        _log("SENDGRID_API_KEY not set — printing digest instead of sending.")
        print(f"Subject: {subject}\n\n{body}")
        return False
    if not to_email:
        _log("NOTIFY_EMAIL not set — cannot address the email.")
        print(f"Subject: {subject}\n\n{body}")
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            plain_text_content=body,
        )
        client = SendGridAPIClient(api_key)
        resp = client.send(message)
        if resp.status_code >= 400:
            _log(f"SendGrid returned HTTP {resp.status_code}")
            return False
        _log(f"email sent to {to_email} (HTTP {resp.status_code}).")
        return True
    except ImportError:
        _log("sendgrid package not installed — run: pip install -r requirements.txt")
        print(f"Subject: {subject}\n\n{body}")
        return False
    except Exception as exc:  # noqa: BLE001 - email send must not crash routine
        _log(f"failed to send email: {exc}")
        return False


def main(argv: list[str]) -> int:
    journal_path = argv[0] if argv else os.path.join(ROOT, "journal", f"{_today_et()}.md")
    subject, body = build_digest(journal_path)
    send_email(subject, body)
    # Always exit 0: a failed email must not fail the end-of-day routine,
    # and the digest has already been logged/printed as a fallback.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
