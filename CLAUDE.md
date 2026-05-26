# telegram_MCP

## Design principle

The jobs pipeline (`it-jobs-poller.py`, `it-jobs-triage.py`, `email-triage.py`) is a **standalone Python application**. It must not depend on Claude Code, the Claude CLI, or any Anthropic-specific tooling being present. LLM calls go through the DeepSeek API (raw HTTP) so the scripts run in any environment — cron, systemd, CI, a server — without a Claude Code session.

## Venv

All scripts use the `jobsmcp` venv: `jobsmcp/bin/python3`. Add dependencies to `requirements.txt` and install with `jobsmcp/bin/pip install -r requirements.txt`.

## Scripts

- `it-jobs-poller.py` — Telethon poller: reads @it_jobs_cyprus and @cyithr via user API, writes to `state/message_queue/`
- `it-jobs-triage.py` — Two-model triage: DeepSeek Flash first-pass → DeepSeek Pro full eval → Telegram Bot API delivery
- `email-triage.py` — Outlook Graph API → DeepSeek Flash header filter → DeepSeek Pro body eval → Telegram Bot API delivery
- `bot-commands.py` — Oneshot bot-command processor: polls getUpdates, handles /briefme, exits
- `lib.py` — Shared utilities: load_env, call_deepseek, extract_json, send_telegram, write_audit, and common constants
- `audit` — CLI for querying the audit trail: `./audit`, `./audit --topology`, `./audit email --today`

## Commit credit

All commits are authored by Filippos Panagiotou. DeepSeek and the Claude Code harness are the toolchain — neither claims authorship.

## State

All runtime state lives in `state/` (project-local). This replaces the deprecated `~/.it-jobs/` directory.
