# Project Architecture

## Overview

Two independent polling-and-triage pipelines that monitor job postings and research emails, filter them with a two-stage DeepSeek pipeline, and deliver matches to a personal Telegram chat. Runs as a systemd user service with no dependency on Claude Code or any Anthropic tooling.

---

## Pipelines

### 1. Telegram group pipeline

```
it-jobs-poller.py  →  message_queue/  →  it-jobs-triage.py  →  Telegram bot
```

**Poller** (`it-jobs-poller.py`)  
Uses a Telethon user-API session to read two groups:

| Group | Queue prefix | Cursor file |
|---|---|---|
| `@it_jobs_cyprus` | `itjobs` | `~/.it-jobs/it-jobs-cursor` |
| `@cyithr` | `cyithr` | `~/.it-jobs/cyithr-cursor` |

New messages are written as JSON files to `~/.it-jobs/message_queue/`. One Telethon session serves both channels.

**Triage** (`it-jobs-triage.py`)  
Reads all `*.json` files from the queue, routes by `chat_id`, applies channel-specific Flash logic, then a shared Pro evaluation:

| Channel | Flash strategy | Pro |
|---|---|---|
| `it_jobs_cyprus` | First 18 lines sent in one call → `flag` or `skip` | Full posting if flagged |
| `cyithr` | One line at a time → `irrelevant`, `relevant`, or `next` (reads next line) | Full posting if relevant or lines exhausted |

---

### 2. Email pipeline

```
Microsoft Graph API  →  email-triage.py  →  Telegram bot
```

**`email-triage.py`** (combined poll + triage — body fetch happens mid-triage, so they cannot be separated)

1. Refreshes the Outlook OAuth token if within 5 minutes of expiry (atomic file write).
2. Fetches up to 50 Inbox messages newer than the cursor via Graph API (headers only).
3. For each message: Flash evaluates From + Subject → `read` or `skip`.
4. If `read`: fetches full body, strips HTML, Pro evaluates → `send` or `skip`.
5. Advances cursor to the last processed message's `receivedDateTime`.

---

## Models

| Stage | Model | Used by |
|---|---|---|
| Flash (fast filter) | `deepseek-v4-flash` | All pipelines |
| Pro (full eval) | `deepseek-v4-pro` | All pipelines |

All LLM calls go directly to `https://api.deepseek.com/v1/chat/completions` via stdlib `urllib` — no SDK dependency.

---

## State directory: `~/.it-jobs/`

```
~/.it-jobs/
├── .env                        # API keys (TELEGRAM_BOT_TOKEN, DEEPSEEK_API_KEY, OUTLOOK_CLIENT_ID, TELEGRAM_API_ID/HASH)
├── it-jobs-criteria.md         # Triage criteria for Telegram groups
├── email-triage-criteria.md    # Triage criteria for email
├── it-jobs-cursor              # Last seen message ID for @it_jobs_cyprus
├── cyithr-cursor               # Last seen message ID for @cyithr
├── email-cursor                # receivedDateTime of last processed email
├── it-jobs-session.session     # Telethon session (created once interactively)
├── outlook-token.json          # Outlook OAuth tokens (access + refresh, auto-refreshed)
├── it-jobs.log                 # Combined stdout/stderr from systemd service
├── it-jobs-triage.jsonl        # LLM audit log — Telegram pipelines
├── email-triage.jsonl          # LLM audit log — email pipeline
└── message_queue/              # Transient queue files written by poller, deleted by triage
    └── {timestamp}-{prefix}-{msg_id}.json
```

---

## Scheduling

```
it-jobs.timer  →  it-jobs.service  (every 2 hours, Persistent=true)
```

The service runs three `ExecStart` steps sequentially:
1. `it-jobs-poller.py` — populates the queue
2. `it-jobs-triage.py` — drains the queue
3. `email-triage.py` — polls and triages email

`Persistent=true` means a missed firing (machine off) runs immediately on next boot.  
`loginctl Linger=yes` ensures the user service starts on boot without a login session.

---

## External dependencies

| Service | Used for | Auth |
|---|---|---|
| Telegram user API (my.telegram.org) | Polling group messages | `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` + interactive OTP (once) |
| Telegram Bot API | Delivering matches | `TELEGRAM_BOT_TOKEN` |
| DeepSeek API | Flash + Pro LLM calls | `DEEPSEEK_API_KEY` |
| Microsoft Graph API | Fetching Outlook email | OAuth2 refresh token in `outlook-token.json` |

---

## Installing on a new machine

```bash
./setup.sh          # creates venv, installs deps, writes systemd units
# fill in ~/.it-jobs/.env
# copy outlook-token.json from previous machine
jobsmcp/bin/python3 it-jobs-poller.py   # one-time Telethon OTP login
systemctl --user start it-jobs.timer
```
