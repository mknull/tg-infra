# it-jobs

Standalone pipeline that monitors job postings and research emails, runs two-stage DeepSeek triage, and forwards matches to your personal Telegram chat.

**No Claude Code or Anthropic tooling required.** Runs under systemd anywhere Python 3.10+ is available.

## How it works

### Telegram group pipeline
1. `it-jobs-poller.py` — Telethon user-API client. Reads new messages from @it_jobs_cyprus, writes them to `~/.it-jobs/message_queue/`.
2. `it-jobs-triage.py` — Two-stage DeepSeek filter:
   - **Flash**: fast filter on the first ~15 lines — is it a relevant vacancy?
   - **Pro**: full evaluation against `~/.it-jobs/it-jobs-criteria.md` — send or skip?

### Email pipeline
3. `email-triage.py` — Fetches new Inbox emails via Microsoft Graph API, two-stage filter:
   - **Flash**: header-only (from + subject) — skip or read body?
   - **Pro**: full body evaluation against `~/.it-jobs/email-triage-criteria.md` — send or skip?

All three scripts run sequentially every 2 hours via the systemd timer.

## Install

```bash
git clone <repo> it-jobs
cd it-jobs
./setup.sh
```

`setup.sh` will:
- Create the `jobsmcp` venv and install dependencies
- Create `~/.it-jobs/` with criteria templates and an empty `.env`
- Generate and install the systemd service + timer (paths substituted at install time)

## Configuration

Fill in `~/.it-jobs/.env`:

```
TELEGRAM_BOT_TOKEN=        # bot that sends matches to your chat
TELEGRAM_API_ID=           # from my.telegram.org (Telethon user API)
TELEGRAM_API_HASH=         # from my.telegram.org
DEEPSEEK_API_KEY=          # from platform.deepseek.com
OUTLOOK_CLIENT_ID=         # Azure app registration client ID
```

Edit the criteria files to match your profile:
- `~/.it-jobs/it-jobs-criteria.md` — Telegram group triage rules
- `~/.it-jobs/email-triage-criteria.md` — email triage rules

## First-run auth

### Telethon (Telegram group polling)
Interactive login required once to create the session file:
```bash
jobsmcp/bin/python3 it-jobs-poller.py
```
Follow the prompts (phone number + OTP). Session saved to `~/.it-jobs/it-jobs-session.session`.

### Outlook (email)
The Outlook OAuth token (`~/.it-jobs/outlook-token.json`) is obtained once via browser-based login. On a new device, copy it from your previous machine:
```bash
scp old-host:~/.it-jobs/outlook-token.json ~/.it-jobs/outlook-token.json
chmod 600 ~/.it-jobs/outlook-token.json
```
The script refreshes the token automatically on each run — you never need to re-authenticate manually.

## Start

```bash
systemctl --user start it-jobs.timer
systemctl --user status it-jobs.timer
```

Logs: `~/.it-jobs/it-jobs.log`  
Audit trails: `~/.it-jobs/it-jobs-triage.jsonl`, `~/.it-jobs/email-triage.jsonl`
