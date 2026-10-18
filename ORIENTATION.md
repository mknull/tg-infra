# Repository orientation

Standalone Python job-market intelligence, scheduled by user-level systemd timers.
DeepSeek handles model calls; Claude Code is development tooling, not a runtime dependency.
See [README.md](README.md) for architecture and [CLAUDE.md](CLAUDE.md) for development conventions.

- **Entry points:** `it_jobs_poller.py` → disk queue → `it_jobs_triage.py` → `lib/delivery.py`.
  `rss_poller.py` adds RSS/Atom feeds to the same queue; see `feeds.json.example`.
  `email_triage.py` handles Outlook. `bot_commands.py` → `agent.py` handles `/briefme`.
  `weekly_trend.py`, `weekly_recovery.py`, and `feedback_poller.py` handle reporting and feedback.
  `setup.sh` generates the service/timer units.
- **Local inputs:** ignored `state/` holds config, credentials, criteria, and runtime records;
  `source/` holds generated profiles; `skills/` holds briefing instructions loaded by the agent.
  A fresh checkout lacks these personalized inputs. Root `*.example` files provide templates.
- **Read-only checks:** `git status --short`, `./audit --health`, `./audit --topology`.
  Audit success alone does not establish live health: inspect timestamps and missing-data messages.
- **Tests:** use the command in `CLAUDE.md` only in a disposable checkout/copy.
  Seed `state/channels.json` from `channels.json.example` as CI does.
  Several tests overwrite/delete runtime state; browser tests can contact services.
  Installer tests stub dependency installation, Docker, and systemd. The full suite is not offline.
- **Known documentation gaps (2026-10-17):** the local wiki describes tracing/replay/probe tools absent from this checkout.
  Matching setup is described in README's “Matching setup” section. Flash and Pro
  now consume direction updates; `state/current-direction.json` is authoritative
  when present, with legacy Markdown fallback.
  Check implementation before relying on these descriptions.
- **Existing work:** inspect Git status before editing; preserve local changes and untracked notes.
  Their presence does not establish whether they are finished or authoritative.
