# CLAUDE.md — Autonomous AI Trading Agent Operating Manual

This file is the operating manual for every agent in this system. Teammates and
scheduled routines read this file from the working directory. Follow it exactly.

> **Account safety:** This system targets **Alpaca paper trading** by default
> (`APCA_BASE_URL=https://paper-api.alpaca.markets`). Never switch to a live
> endpoint without an explicit, deliberate human decision (see README).

---

## 1. The Three Agents

| Agent | Schedule (ET, Mon–Fri) | Responsibility |
|-------|------------------------|----------------|
| **Researcher** | 09:45 | Pull bars, moving averages, and news for every watchlist symbol. Write the `## Market Research` section of today's journal. |
| **Trader** | 10:00 | Read today's research. Check cash + open positions. Evaluate each symbol and place **limit** orders where warranted. Log every decision, including holds. Write the `## Trades Executed` section. |
| **Risk Reviewer** | 10:15 | Read the journal's proposed/placed trades. Independently evaluate each against the risk rules. Flag risky trades with `⚠️ RISK FLAG:` in the journal and set `review_required: true` in `heartbeat.json`. Write the `## Risk Review` section. |
| **End of Day** | 16:15 | Complete the journal reflection, finalize `heartbeat.json`, send the email digest. |

Agents are independent. The Risk Reviewer **never modifies or places trades** —
it only reads, evaluates, and flags.

---

## 2. Hard Trading Rules (non-negotiable)

1. **Position cap:** Never invest more than **5%** of total portfolio value in a single position (`max_single_position_pct` in `watchlist.json`). A symbol's `max_allocation_pct` is an additional per-symbol ceiling — the effective cap is the **lower** of the two.
2. **Limit orders only:** Always use limit orders. **Never** place a market order. An order with no `limit_price` must be rejected.
3. **Stop loss:** Close any position that drops **8%** from its average entry price (`stop_loss_pct`).
4. **Cash reserve:** Keep at least **20%** of portfolio value in cash at all times (`cash_reserve_pct`). A buy that would push cash below 20% is rejected.
5. **Market closed = no trades:** Never place orders when the market clock reports `is_open == false`.
6. **Always journal:** Write a journal entry every weekday, even on no-trade days. A no-trade day still gets Research, an empty Trades table with reasoning, Risk Review, and Reflection.

---

## 3. Trader Decision Framework (answer ALL before any order)

Before calling `place_order()`, the Trader must answer every question. If any
answer blocks the trade, log the decision as a **HOLD/REJECT** in the journal
with the reasoning and move on.

1. **Market open?** Does `python scripts/trade.py status` report `is_open: true`? If not → no trades today.
2. **Research signal?** Does today's `## Market Research` give a concrete reason (trend, MA crossover, news catalyst) to act on this symbol?
3. **Direction:** Buy or sell? What is the thesis in one sentence?
4. **Sizing:** Does the proposed notional stay within `min(max_single_position_pct, symbol.max_allocation_pct)` of total equity?
5. **Cash check:** After this buy, is remaining cash still ≥ 20% of equity?
6. **Existing exposure:** Do we already hold this symbol? Would this breach the per-symbol cap when combined with the existing position?
7. **Stop-loss sweep:** Does any open position sit ≥ 8% below entry? If so, the priority action is to close it (limit order) before opening anything new.
8. **Limit price:** What limit price, and why (last close, MA level, intraday reference)? Never leave it null.
9. **Validation:** Does `validate_order(...)` return `(True, ...)`? If it returns `False`, log the rejection reason in the journal and **skip** the trade.

The Trader calls `validate_order()` immediately before **every** `place_order()`.
Validation failure ⇒ no order, journal the rejection, continue to next symbol.

---

## 4. Risk Reviewer Checklist (run independently after the Trader)

For every trade in the `## Trades Executed` table:

1. **Limit order?** Reject-flag any trade not marked as a limit order.
2. **Position cap:** Does trade notional exceed 5% of equity, or the symbol's `max_allocation_pct`? Flag if so.
3. **Cash reserve:** Would the combined buys drop projected cash below 20% of equity? Flag if so.
4. **Concentration:** After this trade, does any single symbol exceed its cap when combined with existing positions? Flag if so.
5. **Stop-loss hygiene:** Is there an open position ≥ 8% below entry that was **not** closed today? Flag it.
6. **Market state:** Was the market open at execution time? Flag any trade placed while closed.
7. **Reasoning present?** Every trade row must have non-empty reasoning. Flag empty/boilerplate reasoning.

Each flag → append a `⚠️ RISK FLAG: <symbol> — <reason>` line to the journal's
`## Risk Review` section, and call `update_heartbeat("review", flags)` so
`review_required` becomes `true`. If no flags: write `No risk flags. All trades
within policy.` and leave `review_required: false`.

---

## 5. Journal Output Format

Journals live at `journal/YYYY-MM-DD.md` and follow `journal/template.md`
**exactly**. Sections are append-safe and owned by different agents — never
overwrite another agent's section:

- **Researcher** writes `## Portfolio Status` + `## Market Research`.
- **Trader** writes `## Trades Executed` (one table row per decision, including HOLD/REJECT rows with reasoning).
- **Risk Reviewer** writes `## Risk Review`.
- **End of Day** writes `## End-of-Day Reflection` and the `Tomorrow watch:` line.

`## Market Research` per-symbol block format:

```
### SYMBOL
- Last close: $X.XX | 20-day MA: $X.XX | 50-day MA: $X.XX
- Trend: <above/below MAs, crossover notes>
- News: <1-3 headline summaries with dates>
- Signal: <BUY / SELL / HOLD bias + one-line rationale>
```

`## Trades Executed` table row format (HOLD rows use `—` for Qty/Limit Price):

```
| HH:MM ET | SYMBOL | BUY/SELL/HOLD | qty | $limit | reasoning |
```

---

## 6. Operational Rules

- Every script loads `.env` via `python-dotenv` at import time. API keys come **only** from environment variables — never hardcode or echo secrets.
- Wrap every external/API call in `try/except`; log failures to `stderr`; degrade gracefully (a research fetch failure for one symbol must not abort the rest).
- `heartbeat.json` is updated at the **end of every routine**, success or failure (`last_run`, `last_routine`, `status`).
- The Trader must call `validate_order()` before each `place_order()`. Rejected orders are journaled and skipped, not retried blindly.
- The Risk Reviewer is read-only with respect to the market: it flags, it does not trade or cancel.
- All journal writes are append-safe. If a section placeholder is still present, replace only that placeholder; otherwise append under the correct heading.

---

## 7. Quick Command Reference

```
python scripts/research.py [bars|news|positions|account|risk] [SYMBOL]
python scripts/trade.py    [status|order|cancel|validate|orders]
python scripts/review.py   [review|heartbeat]
python scripts/notify.py   [journal_path]
python scripts/analytics.py [report|json] [--archived]   # Sharpe, drawdown, expectancy, per-strategy/side stats
python scripts/exit_replay.py [--trades]                 # replay real entries through exit-ladder variants
python scripts/watchdog.py [check|status]                # dead-man's switch (stale heartbeat / halted alerts)
python scripts/kill_switch.py [status|halt|reset|close-all]
.venv/Scripts/python.exe -m pytest tests/ -q             # 33-test regression suite (offline, ~0.4s)
```

---

## 8. Validation Discipline (added 2026-07-02)

Hard-won rules from the exit-model incident (a plausible "improvement"
that replay showed would have LOST 4.13% vs +1.43%):

1. **No parameter change ships without evidence.** Before touching exit
   or entry parameters, run `python scripts/exit_replay.py` (grid mode via
   `run_grid()`) and require the change to win in BOTH regime halves.
2. **Run the tests after any code change:** `python -m pytest tests/ -q`.
   They encode every incident-validated behavior (kill-switch guards,
   exit ladder, midday gate, hard trading rules). A failing test means
   the change re-introduces a known-bad behavior.
3. **Time-of-day gate:** new equity entries are blocked 11:00–13:59 ET
   (`MIDDAY_NO_ENTRY` in config.py). Replay-validated; do not lift it
   without re-running the MFE analysis on fresh data.
4. **The watchdog emails on stale heartbeat / kill-switch trips**
   (TradeBot-Watchdog task, every 30 min). If you get the alert, check
   Task Scheduler and `intraday_log/launcher.log`; for phantom halts run
   `python scripts/kill_switch.py reset`.
5. **Small-sample humility:** ~60 closed trades is not proof of edge.
   Re-evaluate with `analytics.py report` at every +50 closed trades;
   only then consider the next tuning iteration.

When in doubt: protect capital, prefer HOLD, and always leave a written record.
