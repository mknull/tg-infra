# tg-infra — personal job alerts

This project watches job sources you choose, evaluates vacancies against your
experience and preferences, and sends relevant matches to your Telegram chat.
You supply your own profile; the repository does not come configured for a
particular person or profession.

**The simplest installation needs a Linux machine, a funded DeepSeek API account,
and documents or a written profile. This installer currently delivers through
Telegram, so prepare a Telegram account and bot for that delivery method.** It
follows RSS/Atom feeds. Docker is required only for the default local search service used
by optional researched briefings. Outlook and Telegram group monitoring are also
optional.

You run and maintain the application on your own machine. Setup uses a terminal;
day-to-day interaction happens in Telegram. The machine must remain awake and
online for scheduled work. There is no hosted service, web dashboard or automatic
job application submission. Claude Code is development tooling, not a runtime
requirement.

This README describes the guided installer in
[PR #9](https://github.com/mknull/tg-infra/pull/9). The
[original README](README.md) remains available for architecture and pipeline detail.

- [What you get](#what-you-get)
- [Prepare before setup](#prepare-before-setup)
- [Costs and personal data](#costs-and-personal-data)
- [Optional features and their prerequisites](#optional-features-and-their-prerequisites)
- [Install and review](#install-and-review)
- [After installation](#after-installation)
- [Change settings and troubleshoot](#change-settings-and-troubleshoot)

## What you get

1. **Personalized matching.** Setup reads your CV, letters, notes or written
   profile, asks for preferences, and generates settings you review. The cheaper
   Flash model gets desired and acceptable roles; the more expensive Pro model
   gets detailed evaluation criteria.
2. **Selected sources.** Follow RSS feeds, named Telegram groups/channels, an
   Outlook folder, or a combination. RSS setup can recommend feeds from the
   built-in catalogue or let you choose them yourself. Publisher categories are
   used where available; custom feed URLs are supported.
3. **Filtered Telegram alerts.** Flash screens incoming postings, Pro evaluates
   candidates that pass, and matching jobs arrive in your bot chat. Both stages
   use your reviewed preferences. The aim is to reduce irrelevant alerts and
   expensive model calls.
4. **Adjustments and evidence.** Send `/direction` with updated preferences in
   Telegram. Local audit records let you inspect arrivals, decisions and delivery.
   Optional features add researched briefs and weekly reports.

Coverage depends on the sources you select. The included RSS catalogue contains
FOSSJobs, FSF, Open Source JobHub, We Work Remotely, NoDesk and Tails, with a strong
open-source/technology/remote-work emphasis. It is not a search of the whole job
market. Models can misjudge relevance, and published jobs may already be closed.
See [feed coverage and limits](docs/rss-feed-verification.md).

## Prepare before setup

Prepare these for the current installer, including its Telegram delivery method:

| You need | What to prepare yourself | What setup does |
| --- | --- | --- |
| **Linux host** | Python 3.10+, Git, Python venv/pip, an internet connection and a working user systemd session. Use a normal Linux user. | Creates a Python virtual environment, installs Python packages and, after review, installs user services/timers. |
| **Telegram account** | An account you can use in the Telegram app. Create one first if needed. | Asks you to message your bot to identify the destination chat. |
| **Dedicated Telegram bot** | In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, follow the instructions and keep the token. Use a bot dedicated to this installation. [Telegram's guide](https://core.telegram.org/bots/tutorial#obtain-your-bot-token). | Requests the token, identifies the bot and sends a real test message before activation. |
| **DeepSeek API account** | Create an API key and add API credit at the [DeepSeek platform](https://platform.deepseek.com/). | Uses your key for paid profile generation and ongoing matching. |
| **Your background and preferences** | A folder of supported documents, or a written summary of your experience, desired roles, acceptable alternatives and exclusions. | Generates a profile and matching instructions for your review. |

**Telegram delivery and Telegram input are separate.** Receiving RSS matches
requires your Telegram account and a bot token. Reading Telegram job groups also
requires a user API ID/hash and a separate account login; see below.

On Ubuntu/Debian, prepare the base tools:

```sh
sudo apt-get update
sudo apt-get install git python3 python3-venv python3-pip
python3 --version
systemctl --user show-environment >/dev/null
```

The Python version must be at least 3.10, and the systemctl command must succeed.
Run `./setup.sh` as your normal user, **without sudo**. Setup does not install
system packages or create external accounts for you. Native Windows and macOS
activation are not implemented; Docker packaging of the whole application is
also not implemented.

### Documents or a written profile

Place only the documents you want processed in a folder. Setup reads immediate
files in **TXT, MD, RST and PDF with extractable text** formats. It does not recurse
into subfolders. DOCX and scanned PDFs need conversion first; there is no OCR.
For PDFs, install:

```sh
sudo apt-get install poppler-utils
```

Alternatively, enter `:text` at the document prompt, write your profile, and end
with a line containing only `.`. You do not need to provide a CV file.

Setup reports extraction results and uses the first **8,000 characters per
document** for profile extraction. Put important details in a short note if they
occur later in a long document. Review what the model inferred before accepting
the generated settings.

## Costs and personal data

**Model usage is paid and sends data to DeepSeek.** Setup asks for confirmation
before sending document/profile text and preferences. Ongoing matching sends
posting content and relevant profile/preferences; optional briefs, reports and
direction updates also use model calls. If you enable email input, this includes
job-email content. Original document files remain where you put them.

There is no fixed monthly price or application-wide spending cap. Usage depends
on source volume, initial backfill, how many postings reach Pro, retries and
optional reports. Selecting **All available** RSS entries can create a large
initial batch. Choose narrower feeds and **Future arrivals only** if you want to
avoid evaluating the initial feed inventory. Check your API usage and
[DeepSeek's current pricing](https://api-docs.deepseek.com/quick_start/pricing/).
Keep-existing setup makes no generation calls; generation already performed is
still billable if you later save or cancel the proposal.

Credentials, sessions, queues, job content, logs and audit records live locally in
`state/`; generated profiles in `source/` and `skills/`; briefing output in
`workspace/`. These directories are Git-ignored, which does not encrypt them.
Saved setup proposals also contain private configuration. RSS history has no
automatic pruning, so plan for disk usage and private backups.

Alerts and briefs pass through Telegram; weekly reports pass through Microsoft.
Briefing search queries pass through SearXNG and its upstream search engines, and
the browser contacts the researched websites.

## Optional features and their prerequisites

All of these default **off** on a fresh installation. Basic RSS matching needs
none of the additional services below, although setup installs the repository's
shared Python requirements.

| Feature | What you get | Additional prerequisites |
| --- | --- | --- |
| Telegram input | Jobs from selected Telegram groups/channels | Telegram user API ID/hash, account sign-in and access to the selected sources |
| Outlook input | Jobs from a selected mail folder | Outlook-compatible mailbox, Microsoft Entra app registration and consent |
| Researched `/briefme` | Reply to a job with `/briefme` to receive a researched PDF | SearXNG search, Chromium and PDF system libraries; Docker for the default local SearXNG installation |
| Weekly reports | A weekly market summary emailed to you; replies can update preferences | The same Microsoft app/mailbox setup, even if Outlook job input is disabled |
| Hourly delivery tests | An hourly synthetic Telegram message to check delivery | No additional account; expect these test messages in your chat |

### Telegram groups as an input

Obtain `api_id` and `api_hash` from [Telegram's application page](https://my.telegram.org/apps)
using [Telegram's instructions](https://core.telegram.org/api/obtaining_api_id).
Have your account phone number, login code and two-step-verification password
available as applicable. These credentials are distinct from the bot token.

Setup asks for source usernames, descriptions and typical message format, then
logs in through Telethon and checks read access. The basic wizard accepts
usernames, without `@` or URLs; it does not onboard private invite links. Only
select sources your account can read.

### Outlook input or weekly reports

Prepare an Outlook/Microsoft 365 mailbox accessible through Microsoft Graph and
permission to [register an application in Microsoft Entra](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app).
Your organization's policy may require administrator involvement.

1. Register an app supporting the account you will sign in with. This repository
   uses Microsoft's `common` authority; for an organizational account, use a
   multitenant registration. For a personal Outlook account, include personal
   Microsoft accounts in the supported account types.
2. Enable **Allow public client flows** in the app's authentication settings.
3. Configure Microsoft Graph **delegated** permissions `Mail.Read` and `Mail.Send`.
   The current application requests both, plus `offline_access`, even when only
   email input is enabled. No client secret is used.
4. Keep the **Application (client) ID** and mailbox address for setup. For job
   input, create a dedicated folder and arrange for job-alert emails to reach it;
   setup does not create the folder or email rules.

At Start, setup displays a [device-code sign-in](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)
for you to complete in a browser. It verifies selected-folder/mailbox read access.
The first weekly report's send and delivery remain runtime checks. Whole-Inbox
monitoring requires an explicit advanced override; see the
[installation guide](docs/installation.md#optional-features).

### Researched briefs: Docker, SearXNG, browser and PDF

SearXNG supplies the web search used by `/briefme`. Choose one search arrangement
**before selecting Start**:

- **Local search:** install [Docker Engine for your Linux distribution](https://docs.docker.com/engine/install/)
  and configure [access for your normal user](https://docs.docker.com/engine/install/linux-postinstall/).
  `docker ps` must succeed as the same user running setup, without sudo. Docker
  must be running and localhost port 8080 available. Setup creates a container
  named `searxng`, binds it to `127.0.0.1:8080`, and sets it to restart unless
  stopped. If that container already exists, setup starts and reuses it.
- **Existing search service:** in **Customize → Edit advanced files →
  `state/.env`**, set `SEARXNG_URL` to your service's `/search` endpoint. It must
  support JSON results and return usable searches. Public instances often disable
  JSON; see the [SearXNG API requirements](https://docs.searxng.org/dev/search_api.html).
  A custom endpoint avoids local Docker setup.

For PDF output on Ubuntu/Debian, install:

```sh
sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0
```

Start downloads Chromium through Playwright. If browser system libraries are
missing, setup tells you to run the following after the Python environment exists,
then retry Start:

```sh
jobsmcp/bin/python3 -m playwright install-deps chromium
```

This may request administrator access to install OS packages. Setup checks that
the browser launches, search returns results, and a PDF can be generated. These
downloads and the search container are additional host resources; disabling
briefings later does not remove them. SearXNG still depends on upstream search
availability.

## Install and review

For a **new checkout of the guided-setup PR**, after preparing the required items:

```sh
git clone --branch feat/guided-setup https://github.com/mknull/tg-infra.git
cd tg-infra
./setup.sh
```

For an **existing checkout**, check your local changes before switching branches.
With GitHub CLI installed, `gh pr checkout 9` selects this PR; then run
`./setup.sh`. Cloning or switching branches alone starts no services.

The wizard creates `jobsmcp/`, installs Python dependencies and walks through:

| Step | What to expect |
| --- | --- |
| Choose features | RSS is selected by default. Keep optional features off for the shortest setup. At least one job input is required. |
| Enter credentials | Bot token, DeepSeek key and your name; additional credentials only for selected features. |
| Supply documents/preferences | Folder path or `:text`, then role preferences and confirmation to use the paid model API. |
| Choose RSS feeds | Recommendations from the catalogue or manual selection, with URLs/coverage for review. Choose Recent 30 days, Future arrivals only or All available for initial intake. |
| Review matching | Inspect the generated profile, Flash role preferences and Pro criteria. Apply accepts them into a private proposal. |
| Review installation | Choose **Start**, **Customize**, **Save for Later** or **Quit**. |

The opening notice appears before Python package installation. Extra requirements
appear beside feature choices. Before paid generation, setup checks user systemd,
schedules and, for briefings, PDF libraries and Docker access for local search.
Missing prerequisites return you to Customize/Save without starting generation
or downloading Chromium. After correcting them, use **Customize → Matching** to
retry. Browser system libraries and live account access are verified at Start.

**Start** prepares selected optional dependencies, authenticates inputs, pairs
your Telegram chat if needed, verifies input access and sends a real delivery
test. After those checks pass, it applies configuration and enables scheduled
processing. Paid generation happens earlier; ongoing paid matching begins once
the scheduled workers run.

**Save for Later** retains a private proposal without changing active settings
or schedules. Run `./setup.sh` again to Resume. **Quit** discards a new unsaved
proposal. During reinstall, existing timers pause for verification and their
previous state is restored if verification fails.

Setup does not supply job-source accounts, purchase API credit, install Docker
or create a Microsoft app. Those are the preparation steps above.

## After installation

Expect a setup test message immediately. Real matches depend on source content,
your backfill choice and matching decisions. With default schedules, RSS wakes
hourly at minute 10 and queued job matching runs at minutes 20 and 50. Individual
feeds have their own polling cadence. Bot commands are checked every 30 seconds;
they are not necessarily answered instantly.

Check configuration, scheduling and recorded processing from the checkout:

```sh
jobsmcp/bin/python3 setup_verify.py status
systemctl --user list-timers
./audit --health
```

A successful setup proves the selected access checks and test delivery worked.
It does **not** prove a real vacancy has completed the pipeline. Audit checks
recorded arrivals, processing and delivery; inspect it after scheduled work has
occurred. It does not contact providers or judge the relevance of your chosen
sources. An empty feed or no matches can be legitimate.

For operation after logout and at boot, enable lingering for your Linux user:

```sh
sudo loginctl enable-linger "$USER"
```

Keep the machine running and leave the checkout at its installation path; the
services refer to it. Use one installation per Linux user because service names
are shared within that account. Telegram is the supported interactive interface;
Signal and Matrix are not implemented.

## Change settings and troubleshoot

Run `./setup.sh` again to review changes. **Keep existing** is the default and
makes no generation calls. Choose **Customize → Matching → Complete missing
setup** for missing settings, or **Regenerate** for updated documents/preferences.
Existing queued jobs, processed history and direction updates are retained.
Disabling an input pauses its queued work; re-enabling it makes that work eligible
again. `/direction` updates matching preferences but does not reselect RSS feeds.

Advanced controls are available through **Customize → Edit advanced files**:

| File | Controls |
| --- | --- |
| `state/installation.json` | Features and timer schedule overrides |
| `state/feeds.json` | RSS URLs, categories/selectors, polling cadence, backfill and Flash roles |
| `state/channels.json` | Telegram sources, descriptions, enabled flags and Flash roles |
| `state/*-criteria.md` | Pro's detailed matching instructions |
| `state/.env` | Credentials, search endpoint and other environment settings |

If generation fails, setup reports the failure and offers a retry or deferral;
missing required matching blocks Start. If authentication or delivery fails,
correct the relevant account/configuration and retry. Detailed recovery and
saved-proposal instructions are in the [installation guide](docs/installation.md).

To stop future scheduled work while retaining your data:

```sh
systemctl --user disable --now \
  bot-commands.timer telegram-poll.timer rss-poll.timer job-triage.timer \
  email-ingest.timer weekly-trend.timer weekly-recovery.timer \
  feedback-poller.timer delivery-canary.timer
```

An already-running worker may finish. This also stops bot command polling, so
resume through `./setup.sh` → Start. A local SearXNG container runs independently
of these timers.

For implementation detail, see the [original README](README.md) and
[repository orientation](ORIENTATION.md). For what has actually been tested, see
[installer verification and its limits](docs/setup-verification.md): automated
provider fixtures and real systemd checks do not establish every feature against
live accounts or a pristine operating system. Development tests must run in a
disposable copy as explained in [CLAUDE.md](CLAUDE.md), not against personal state.
