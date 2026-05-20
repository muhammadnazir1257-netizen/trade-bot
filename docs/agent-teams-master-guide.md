# Agent Teams — Master Reference Guide

> Source: https://code.claude.com/docs/en/agent-teams
> Compiled for the **trade bot** project to inform future agent-team design.
> Status: **Experimental**. Requires Claude Code **v2.1.32+**. Enabled in this repo via `.claude/settings.local.json` (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`).

---

## 1. What agent teams are

Multiple Claude Code instances working together:

- **Team lead** — the session that creates the team, spawns teammates, assigns work, synthesizes results. Lead is fixed for the team's lifetime (no promotion/transfer).
- **Teammates** — independent Claude Code sessions, each with its own context window. They claim tasks and message each other directly.
- **Task list** — shared work items teammates claim and complete; supports dependencies.
- **Mailbox** — messaging system; messages delivered automatically (no polling).

You can talk to any teammate directly — not just through the lead.

---

## 2. Agent teams vs. subagents (decision rule)

| | Subagents | Agent teams |
|---|---|---|
| Context | Own window; result returns to caller | Own window; fully independent |
| Communication | Report back to main agent only | Teammates message each other directly |
| Coordination | Main agent manages all work | Shared task list, self-coordination |
| Best for | Focused tasks, only result matters | Complex work needing discussion/collaboration |
| Token cost | Lower (summarized back) | Higher (each teammate = full instance) |

**Use a team only when workers must communicate/challenge each other.** For sequential work, same-file edits, or heavy dependencies → single session or subagents.

---

## 3. Enablement & configuration

`.claude/settings.local.json` (already set in this repo):
```json
{ "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" } }
```

**Display modes** (`teammateMode` in `~/.claude/settings.json`, or `--teammate-mode <mode>`):
- `in-process` — all teammates in main terminal. Cycle with **Shift+Down**; Enter to view; Esc to interrupt; Ctrl+T toggles task list. Works in any terminal.
- `tmux` — split panes (tmux or iTerm2 `it2` CLI). Not supported in VS Code integrated terminal, Windows Terminal, or Ghostty.
- `auto` (default) — split panes if already inside tmux, else in-process.

> **This project runs on Windows + VS Code terminal → use `in-process` mode.** Split panes are unavailable here.

**Default teammate model**: set via `/config` → "Default teammate model". Teammates do *not* inherit the lead's `/model` unless set to "Default (leader's model)". Can also specify per-prompt ("Use Sonnet for each teammate").

**Storage** (auto-generated, do not hand-edit):
- Team config: `~/.claude/teams/{team-name}/config.json` (runtime state: session IDs, pane IDs, `members` array)
- Task list: `~/.claude/tasks/{team-name}/`

No project-level team config exists; `.claude/teams/*.json` is treated as an ordinary file, not config.

---

## 4. Operating a team

- **Start**: ask Claude in natural language to create a team; describe task + roles. Claude spawns teammates and coordinates. You always confirm before a team is created (whether you request it or Claude proposes it).
- **Specify teammates**: state count and model explicitly if desired.
- **Plan approval gate**: ask for "require plan approval" — teammate stays in read-only plan mode until lead approves. Lead decides autonomously; steer it with criteria in your prompt (e.g., "only approve plans with test coverage").
- **Direct messaging**: each teammate is a full session — message any by name to redirect.
- **Task assignment**: lead assigns explicitly, or teammates self-claim next unblocked task. File locking prevents claim races. Dependencies auto-unblock on completion.
- **Shut down a teammate**: "Ask the X teammate to shut down" (teammate may approve or reject).
- **Clean up**: "Clean up the team" — **always via the lead**; fails if teammates still running, so shut them down first.

### Subagent definitions as teammate roles
Reference a subagent type by name when spawning: *"Spawn a teammate using the security-reviewer agent type…"*. Teammate honors that definition's `tools` allowlist and `model`; its body is appended to the system prompt. `SendMessage` + task tools always available. **Not applied**: the definition's `skills` and `mcpServers` frontmatter — teammates load skills/MCP from project & user settings like a normal session.

### Quality gates via hooks
- `TeammateIdle` — runs before teammate goes idle; exit code 2 sends feedback, keeps it working.
- `TaskCreated` — exit 2 blocks creation + sends feedback.
- `TaskCompleted` — exit 2 blocks completion + sends feedback.

### Permissions
Teammates inherit the lead's permission mode at spawn (incl. `--dangerously-skip-permissions`). Per-teammate mode can be changed *after* spawn, not at spawn time. Pre-approve common ops to reduce prompt friction (requests bubble up to lead).

---

## 5. Strong use cases

- **Research & review** — parallel investigation, then share/challenge findings.
- **New modules/features** — each teammate owns a separate piece.
- **Debugging with competing hypotheses** — adversarial teammates disprove each other (counters anchoring bias).
- **Cross-layer coordination** — frontend/backend/tests, one owner each.

Example prompts:
```text
Create an agent team to review PR #142. Spawn three reviewers:
- one on security, one on performance, one on test coverage.
Have them each review and report findings.
```
```text
Users report the app exits after one message. Spawn 5 teammates to
investigate different hypotheses. Have them talk to each other to
disprove each other's theories, like a scientific debate. Update the
findings doc with the consensus.
```

---

## 6. Best practices

- **Give context in the spawn prompt** — teammates load CLAUDE.md/MCP/skills but NOT the lead's conversation history. Include task specifics, file paths, constraints, and expected deliverable + severity format.
- **Team size**: start with **3–5 teammates**. Aim for **5–6 tasks per teammate**. 15 independent tasks → ~3 teammates. Three focused teammates beat five scattered ones.
- **Task sizing**: self-contained units with a clear deliverable (a function, a test file, a review). Too small → coordination overhead; too large → long unchecked runs.
- **Wait for teammates**: if the lead starts doing the work itself: *"Wait for your teammates to complete their tasks before proceeding."*
- **Start with research/review** before parallel implementation.
- **Avoid file conflicts** — give each teammate a disjoint set of files.
- **Monitor & steer** — check progress, redirect, synthesize as findings arrive. Don't run unattended too long.
- **Predictable names** — tell the lead what to call each teammate so you can reference them later.
- **CLAUDE.md works normally** — teammates read it from their working directory; use it for shared project guidance.

---

## 7. Troubleshooting

- **Teammates not appearing**: in-process — press Shift+Down to cycle; verify task was complex enough; for split panes check `which tmux` / iTerm2 `it2` + Python API.
- **Too many permission prompts**: pre-approve common operations before spawning.
- **Teammates stop on errors**: view output (Shift+Down / pane), give instructions, or spawn a replacement.
- **Lead shuts down early**: tell it to keep going / wait for teammates.
- **Orphaned tmux sessions**: `tmux ls` then `tmux kill-session -t <name>` (N/A in this Windows/VS Code setup).

---

## 8. Limitations (experimental)

- **No session resumption with in-process teammates** — `/resume` and `/rewind` don't restore them; lead may message dead teammates → tell it to spawn new ones. (Directly relevant: this project uses in-process mode.)
- **Task status can lag** — teammates may fail to mark tasks complete, blocking dependents; verify and update manually or nudge the lead.
- **Shutdown can be slow** — teammates finish current request/tool call first.
- **One team at a time** — clean up before creating a new team.
- **No nested teams** — teammates can't spawn teams/teammates.
- **Lead is fixed** — no promotion or leadership transfer.
- **Permissions set at spawn** — no per-teammate modes at spawn time.
- **Split panes need tmux/iTerm2** — unsupported in VS Code terminal, Windows Terminal, Ghostty.

---

## 9. Project-specific notes (trade bot)

- Environment: Windows 11 + VS Code integrated terminal → **in-process mode only**; no tmux/split panes.
- Because in-process teammates don't survive `/resume`/`/rewind`, prefer **shorter team sessions** and synthesize results into files (e.g., `docs/`) before ending.
- Token cost scales linearly per teammate — reserve teams for genuinely parallel research/review/feature work on the bot; use a single session or subagents for sequential trading-logic changes and same-file edits.
- Keep teammates on disjoint files (e.g., strategy module vs. data layer vs. tests) to avoid overwrite conflicts.
