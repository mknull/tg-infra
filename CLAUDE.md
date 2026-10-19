# telegram_MCP

Start here: [repository orientation and safe verification](ORIENTATION.md).

## Design principle

The jobs pipeline is a **standalone Python application**. It must not depend on Claude Code, the Claude CLI, or any Anthropic-specific tooling being present. LLM calls go through the DeepSeek API (raw HTTP) so the scripts run in any environment — cron, systemd, CI, a server — without a Claude Code session.

## Venv

All scripts use the `jobsmcp` venv: `jobsmcp/bin/python3`. Add dependencies to `requirements.txt` and install with `jobsmcp/bin/pip install -r requirements.txt`.

## Scripts

- `it_jobs_poller.py` — Telethon poller: reads Telegram groups via user API, writes to `state/message_queue/`
- `rss_poller.py` — RSS/Atom poller: configured in `state/feeds.json`, persistent per-feed index, writes to the same queue
- `it_jobs_triage.py` — Two-model triage: incremental 3-line Flash → Pro full eval → delivery
- `email_triage.py` — Outlook Graph API → Flash header filter → Pro body eval → delivery
- `bot_commands.py` — Telegram bot: /briefme, /direction, /start, /status, /help
- `weekly_trend.py` — Weekly market trend report + smell investigation (idempotent: `ensure_week_report`)
- `weekly_recovery.py` — Retries the weekly report every 2h until confirmed sent (fail-closed, ledger-backed)
- `outlook_auth.py` — One-time Outlook device-code OAuth flow
- `feedback_poller.py` — Polls Outlook for replies to weekly reports
- `generate_profile.py` — Two-stage profile generation from user documents
- `lib/` — Shared utilities: config, api (DeepSeek), delivery, direction, auth, audit
- `audit` — CLI: `./audit`, `./audit --topology`, `./audit --health`, `./audit email --today`

## Tests

Tests live in `tests/`. Run the suite in a disposable copy (some older tests modify project state):
```
jobsmcp/bin/python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

The RSS fixtures, process-level pipeline and installer tests use temporary state
and mocked or loopback external services:
`jobsmcp/bin/python3 -m unittest tests.test_rss tests.test_rss_e2e tests.test_system_setup`.
The process-level tests require permission to bind a loopback socket.

CI runs the full suite + `./audit --health` on every push.

## Commit credit

All commits are authored by Filippos Panagiotou. DeepSeek and the Claude Code harness are the toolchain — neither claims authorship.

## Branch workflow

One branch per PR. After a PR merges, close out the branch — delete it locally and on the remote, and fast-forward local `master` to `origin/master` — then cut the next branch fresh from the updated `master`. Do not stack new work onto a branch that has already been merged: it makes the "what's actually new" comparison ambiguous and lets local `master` drift behind the remote.

## State

Runtime state lives in `state/` under `TG_INFRA_DATA_DIR` (defaults to the checkout).
This replaces the deprecated `~/.it-jobs/` directory. Setup proposals are private
copies under `state/setup/`; do not treat saved proposals as active installations.
