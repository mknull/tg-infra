# telegram_MCP

## Design principle

The jobs pipeline is a **standalone Python application**. It must not depend on Claude Code, the Claude CLI, or any Anthropic-specific tooling being present. LLM calls go through the DeepSeek API (raw HTTP) so the scripts run in any environment — cron, systemd, CI, a server — without a Claude Code session.

## Venv

All scripts use the `jobsmcp` venv: `jobsmcp/bin/python3`. Add dependencies to `requirements.txt` and install with `jobsmcp/bin/pip install -r requirements.txt`.

## Scripts

- `it-jobs-poller.py` — Telethon poller: reads Telegram groups via user API, writes to `state/message_queue/`
- `it-jobs-triage.py` — Two-model triage: incremental 3-line Flash → Pro full eval → delivery
- `email-triage.py` — Outlook Graph API → Flash header filter → Pro body eval → delivery
- `bot-commands.py` — Telegram bot: /briefme, /direction, /start, /status, /help
- `weekly-trend.py` — Weekly market trend report + smell investigation (idempotent: `ensure_week_report`)
- `weekly-recovery.py` — Retries the weekly report every 2h until confirmed sent (fail-closed, ledger-backed)
- `outlook-auth.py` — One-time Outlook device-code OAuth flow
- `feedback-poller.py` — Polls Outlook for replies to weekly reports
- `generate-profile.py` — Two-stage profile generation from user documents
- `lib/` — Shared utilities: config, api (DeepSeek), delivery, direction, auth, audit
- `audit` — CLI: `./audit`, `./audit --topology`, `./audit --health`, `./audit email --today`

## Tests

242 tests across 21 files in `tests/`. Run with:
```
jobsmcp/bin/python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

CI runs the full suite + `./audit --health` on every push.

## Commit credit

All commits are authored by Filippos Panagiotou. DeepSeek and the Claude Code harness are the toolchain — neither claims authorship.

## Branch workflow

One branch per PR. After a PR merges, close out the branch — delete it locally and on the remote, and fast-forward local `master` to `origin/master` — then cut the next branch fresh from the updated `master`. Do not stack new work onto a branch that has already been merged: it makes the "what's actually new" comparison ambiguous and lets local `master` drift behind the remote.

## State

All runtime state lives in `state/` (project-local). This replaces the deprecated `~/.it-jobs/` directory.
