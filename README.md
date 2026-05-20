# Autonomous AI Trading Agent System

A three-agent system that researches, trades, and risk-reviews on a schedule
against the Alpaca paper-trading API. The agents are coordinated by Claude
Code routines; every action is journaled in Markdown so you can read, learn
from, and improve the system over time.

> **Defaults to paper trading.** Live trading requires a deliberate, manual
> change documented below.

## Architecture

```
                       ┌──────────────────────────────┐
                       │       watchlist.json         │
                       │   (symbols & risk policy)    │
                       └──────────────┬───────────────┘
                                      │
            09:45 ET ─►  ┌────────────▼────────────┐
                         │   Researcher (Agent 1)  │   bars + MAs + news
                         │   scripts/research.py   │   → ## Market Research
                         └────────────┬────────────┘
                                      ▼
            10:00 ET ─►  ┌─────────────────────────┐
                         │     Trader (Agent 2)    │   reads research,
                         │     scripts/trade.py    │   validates, places
                         │   (LIMIT ORDERS ONLY)   │   → ## Trades Executed
                         └────────────┬────────────┘
                                      ▼
            10:15 ET ─►  ┌─────────────────────────┐
                         │ Risk Reviewer (Agent 3) │   independent audit,
                         │    scripts/review.py    │   ⚠️ RISK FLAGs
                         │      (read-only)        │   → ## Risk Review
                         └────────────┬────────────┘     + heartbeat.json
                                      ▼
            16:15 ET ─►  ┌─────────────────────────┐
                         │      End of Day         │   reflection +
                         │    scripts/notify.py    │   email digest
                         └─────────────────────────┘
                                      │
                                      ▼
                             journal/YYYY-MM-DD.md
```

`heartbeat.json` is the system health file; every routine updates it with
`last_run`, `last_routine`, `status`, `review_required`, and `flags`.

## Repository layout

```
.
├── CLAUDE.md                # operating manual (READ FIRST)
├── README.md                # this file
├── watchlist.json           # symbols + risk policy
├── heartbeat.json           # system state
├── requirements.txt
├── .env.example
├── .claude/
│   ├── settings.local.json  # local Claude Code config
│   └── routines.json        # scheduled routines
├── docs/
│   └── agent-teams-master-guide.md
├── journal/
│   ├── template.md          # daily journal template
│   └── YYYY-MM-DD.md        # one file per trading day
└── scripts/
    ├── research.py          # Researcher tools
    ├── trade.py             # Trader tools
    ├── review.py            # Risk Reviewer tools
    └── notify.py            # EOD digest emailer
```

## 1. Alpaca paper-trading setup

1. Create an account at https://alpaca.markets (paper trading is free, no
   funding required).
2. Sign in → **Paper Trading** → **API Keys** → **Generate New Key**. Copy
   both the Key ID and the Secret — the secret is shown only once.
3. Optional: sign in to **SendGrid** (free tier) and create an API key with
   the **Mail Send** scope. Verify a sender address in **Settings → Sender
   Authentication** — SendGrid will not deliver mail from an unverified
   `from:` address.

## 2. Python environment

Requires Python 3.10+ (uses `from __future__ import annotations`, `dict[str, X]`
syntax, and `zoneinfo`).

```powershell
# from the project root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy the env template and fill in real values:

```powershell
copy .env.example .env
notepad .env
```

Smoke-test the connection (should print JSON; no orders placed):

```powershell
python scripts/research.py account
python scripts/trade.py status
```

## 3. Claude Code routines

The routines that drive the agents live in [.claude/routines.json](.claude/routines.json).
Activate them with the bundled scheduler skill:

```text
/schedule
```

…and follow the prompts to create each routine from the JSON. The four
routines are:

| Routine | Cron (ET) | Script(s) it drives |
|---|---|---|
| Morning Research | `45 9 * * 1-5` | `scripts/research.py` |
| Trading Session | `0 10 * * 1-5` | `scripts/trade.py` |
| Risk Review | `15 10 * * 1-5` | `scripts/review.py` |
| End of Day | `15 16 * * 1-5` | `scripts/notify.py` |

Manual test runs (each is safe — no orders placed by the research or review
commands):

```powershell
python scripts/research.py account
python scripts/research.py bars SPY
python scripts/trade.py status
python scripts/review.py review
python scripts/notify.py
```

The Trader's `validate` command performs a full risk pre-flight without
placing an order:

```powershell
python scripts/trade.py validate SPY 1 buy 500
```

## 4. Reading the journal

Each trading day produces a single Markdown file at `journal/YYYY-MM-DD.md`
with four sections owned by different agents (see [CLAUDE.md](CLAUDE.md)
section 5). The file is **append-safe** — each agent writes only its own
section.

Look for:

- `⚠️ RISK FLAG:` lines under `## Risk Review` — the Reviewer's findings.
- `Tomorrow watch:` line at the bottom — what the EOD agent thinks deserves
  attention next session.
- HOLD/REJECT rows in `## Trades Executed` — every skipped trade has a
  reason. Patterns there are the best signal for tuning policy.

Use the journal to grow the system over time:

1. After each week, skim the HOLD reasons for the same blocker recurring.
2. If a policy threshold is too strict, edit `watchlist.json` (per-symbol
   `max_allocation_pct`, `cash_reserve_pct`, `stop_loss_pct`,
   `max_single_position_pct`). Commit the change with a note explaining why.
3. If the Researcher's signals consistently miss something (e.g. an earnings
   catalyst), tighten the instructions in `CLAUDE.md` and the Researcher
   routine's prompt — both are loaded fresh on every run.
4. The Risk Reviewer is the watchdog — extend its checklist in
   `CLAUDE.md` section 4 and add a matching rule in
   `scripts/review.py:evaluate_trade_risk()`.

## 5. Going live (DANGER)

⚠️ **Stop and read this entire section before changing a single character.**

This system trades on **paper** by default. To trade with real money you must:

1. Verify the paper system has been running cleanly for **weeks**, with no
   recurring `⚠️ RISK FLAG`s, no validation bypasses, and P&L behavior you
   actually understand.
2. Generate **live** API keys in Alpaca (separate from your paper keys).
3. Edit `.env`:
   - `APCA_API_KEY_ID` → live key
   - `APCA_API_SECRET_KEY` → live secret
   - `APCA_BASE_URL=https://api.alpaca.markets`   ← **note: no `paper-`**
4. Lower your watchlist caps before the first live run. Real-money positions
   should start dramatically smaller than the policy maxima.
5. Re-run the smoke tests above and watch the first session live, by hand.

There is no kill switch built in beyond the validation gate and the cron
schedule. The simplest emergency stop is to disable the routines (`/schedule`)
and run `python scripts/trade.py cancel` to flatten open orders.

To go back to paper, flip the three env vars back. Keep paper and live keys
in separate `.env` files (e.g. `.env.live`) so a typo can't promote the
account silently.

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `APCA_API_KEY_ID … not set` | `.env` missing or in wrong directory | Run scripts from the project root. |
| `HTTP 403` on data calls | Free account, wrong feed | Set `ALPACA_DATA_FEED=iex` (default). |
| `HTTP 422` placing order | Limit price too far from market or qty 0 | Inspect the validation reason; adjust the limit. |
| `zoneinfo` errors on Windows | Missing tzdata | `pip install tzdata` (already in requirements). |
| No email sent | `SENDGRID_API_KEY` unset | `notify.py` prints the digest to stdout as a dry run. Set the key and a verified sender. |

When something behaves unexpectedly: read the journal first, then
`heartbeat.json`, then re-run the failing command directly. Every script is
safe to invoke by hand.
