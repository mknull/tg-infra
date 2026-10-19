# Install and configure

The simplest installation follows RSS job feeds and delivers relevant vacancies
to your private Telegram bot chat. Telegram groups, Outlook input, researched
briefings, weekly email reports and hourly test messages are optional.

## 1. Prepare the host and accounts

Use a Linux machine with Python 3.10+ and a working **user systemd manager**.
Run setup as your normal user, from a terminal; do not run it with `sudo`.
On Ubuntu/Debian, install the base tools:

```sh
sudo apt-get update
sudo apt-get install git python3 python3-venv python3-pip
```

Prepare:

- A funded [DeepSeek API account](https://platform.deepseek.com) and API key.
- A dedicated Telegram bot: open **@BotFather** in Telegram, send `/newbot`,
  follow its instructions and keep the token. Setup later identifies the exact
  bot you must message. RSS input does **not** need a Telegram API ID/hash.
- Your **CV, letters and/or notes**, in a folder on this machine. Setup reads
  immediate files in PDF (extractable text), TXT, MD and RST formats. For PDF,
  install `sudo apt-get install poppler-utils`. Scans need conversion to text;
  DOCX is unsupported. Alternatively, write a profile directly during setup.

Documents, profile text and preferences are sent to DeepSeek after confirmation.
Generation and scheduled matching incur API charges. Original documents remain
where you placed them; generated profiles and credentials stay in ignored local
directories. Each document contributes its first 8,000 characters; setup reports
extraction and warns about truncation. Use shorter notes for essential details.

## 2. Run the wizard

```sh
git clone https://github.com/mknull/tg-infra.git
cd tg-infra
./setup.sh
```

Setup creates `jobsmcp`, installs Python dependencies, then guides you through:

1. **Features.** Accept RSS for the shortest path. All optional features default
   off on a fresh install. Every configuration must have at least one input.
2. **Credentials.** Enter the bot token, DeepSeek key and your name. Additional
   credential questions appear only for selected features. Secrets are masked
   in an interactive terminal and omitted from the review.
3. **Documents and preferences.** Enter the folder path, or `:text` and write
   your experience/interests, ending with a line containing only `.`. Add roles,
   alternatives and exclusions. Confirm generation after reviewing the notice.
4. **RSS choices.** Accept recommendations from the fixed catalogue or choose
   feeds manually. Review coverage, URLs and the initial inventory. Choose
   recent jobs, future arrivals only, or all available entries. Larger backfills
   can increase matching costs.
5. **Matching review.** Inspect the interpreted profile, Flash's desired and
   acceptable roles, and Pro's detailed criteria. Apply accepts these into the
   private installation proposal; Revise changes preferences before acceptance.
6. **Installation review.** Choose Start, Customize, Save for Later, or Quit.

**Start** authenticates selected inputs, checks read access, asks you to message
your bot if pairing is needed, and sends a real test message. It then activates
only the selected timers. Existing timers pause during these checks and resume
their previous state if verification fails. Busy workers are allowed to finish.

**Save for Later** saves the proposal privately without changing active settings
or services. Run `./setup.sh` again and choose Resume. **Quit** discards a new,
unsaved proposal. If a resumed proposal is cancelled, the saved copy remains.

## 3. Confirm operation

After Start succeeds:

```sh
jobsmcp/bin/python3 setup_verify.py status
systemctl --user list-timers
./audit --health
```

For operation after logout and at boot, enable lingering once:

```sh
sudo loginctl enable-linger "$USER"
```

Setup verifies access and test delivery. It does **not** claim that a real job
has arrived, been evaluated or been delivered. Check audit after scheduled work
occurs. RSS wakes hourly at minute 10; queued triage runs at minutes 20 and 50.
Feeds retain their own cadence. A quiet or empty feed need not produce a match.
Audit reads arrival, processing and delivery evidence; it does not contact sites
or judge whether your source choices or matching preferences are suitable.

## Optional features

| Feature | Additional preparation and verification |
| --- | --- |
| Telegram input | Get API ID/hash from [Telegram's application page](https://my.telegram.org/apps). Supply group usernames and descriptions. Setup prompts for phone/code/2FA as needed and checks read access. Use groups accessible to your account. |
| Outlook job input | Register a Microsoft Entra app supporting your account, enable public-client flows, and configure delegated `Mail.Read` and `Mail.Send`. Supply client ID, mailbox address and a dedicated folder containing job alerts. Follow Microsoft's device-code sign-in shown by setup. Setup checks folder access; selecting the entire Inbox requires `OUTLOOK_FOLDER_CONFIRM=usemyinboxitsfine` in advanced configuration. |
| Weekly reports | Uses the same Outlook app/authentication independently of job-email input. Setup checks mailbox read access. The first report's send and confirmation remain runtime checks. |
| Researched `/briefme` | Needs Chromium, PDF system libraries and working SearXNG search. Start downloads Chromium. For local search, Docker must already work for your user (`docker ps`); setup starts/creates `searxng` on localhost:8080. Alternatively configure `SEARXNG_URL`. Setup checks browser launch, search results and PDF generation. |
| Hourly delivery tests | Sends hourly synthetic Telegram messages. Independent of the one-time setup delivery test. |

For briefings on Ubuntu/Debian, install PDF libraries with
`sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`.
If Chromium reports missing libraries, run
`jobsmcp/bin/python3 -m playwright install-deps chromium`, then retry Start.
These optional system dependencies are not required for ordinary RSS matching.

## Reinstall and advanced controls

Run `./setup.sh` again. **Keep existing** is the default and makes no model calls.
Choose Start to repeat verification and apply the reviewed feature selection.
Existing queue entries, cursors, histories, delivery records, custom URLs and
direction updates are retained. Disabling an input pauses its queued jobs;
re-enabling it makes that work eligible again. Legacy installations show inferred
feature choices for review before adopting the explicit configuration.

Use **Customize → Matching → Complete missing setup** for new inputs or missing
profiles. Use **Regenerate** when your information changes; review replacements.
`/direction` continues to update Flash and Pro without reselecting feeds.

**Customize → Edit advanced files** uses `$VISUAL`, `$EDITOR`, or `nano`:

| File | Controls |
| --- | --- |
| `state/installation.json` | Feature booleans and `schedules` overrides by timer name, e.g. `"rss-poll": "hourly"`. Calendar syntax is checked before Start. |
| `state/feeds.json` | Catalogue/custom URLs, per-subscription cadence, selectors, exclusions, backfill and Flash roles. Keep publisher IDs stable to preserve history. |
| `state/channels.json` | Telegram source descriptions, Flash roles and per-source `enabled` flags. |
| `state/*-criteria.md` | Pro's detailed matching instructions. |
| `state/.env` | Credentials, `SEARXNG_URL`, folder confirmation and existing custom keys. |
| `state/delivery.json`, when present | Existing delivery routing controls. |

Edited JSON and configuration are validated before use. Custom systemd units
are preserved with warnings; conflicting schedule overrides are rejected.
Unmodified generated units can be updated, with `.pre-setup` backups. Use one
installation per Linux user: service names are shared within that account.

Keep this checkout in its installation location: generated services refer to it.
The Python wizard and Linux activation adapter are separate. Docker packaging,
installation relocation and schedulers for other operating systems are outside
this change.

## When a step fails

- **Model error or invalid JSON:** setup explains the failure, offers Retry/Later,
  and blocks Start while required matching is missing. Flash structured output
  gets at most one correction attempt. Saved proposals retain the matching log
  at `state/setup/proposal-*/state/setup.log`; no partial matching is published.
- **Unreadable documents:** correct the path/format or use `:text`, then choose
  Customize → Matching. An empty or unextractable input cannot produce a profile.
- **Input or delivery check fails:** correct credentials/access or retry Start.
  Existing service state is restored. OAuth token refreshes are retained for the
  same account even when the configuration change fails.
- **Saved proposal is stale:** choose New proposal on the next run; setup refuses
  to overwrite settings changed since the proposal was created.
- **No user systemd manager:** use a normal Linux login session and retry. Save
  remains available without systemd. This version cannot activate on macOS or
  native Windows.
- **Rollback needs attention:** setup names the systemctl error. Inspect
  `systemctl --user status <name>.timer <name>.service` before retrying; setup
  cannot restore a manager that is unavailable. Multi-file application is not
  guaranteed atomic across power loss or a forced process kill.

See [verification coverage and limits](setup-verification.md).
