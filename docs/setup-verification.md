# Guided setup verification

Checked on 2026-10-19. Scope: a working guided installer on Linux; Docker
packaging and schedulers for other operating systems are not implemented.

| Scenario | Evidence |
| --- | --- |
| All seven nonempty RSS/Telegram/Outlook input combinations | Process tests run the real shell bootstrap, wizard, generation, verification and unit adapter; assert only required credentials/authentication, criteria and timers. |
| Documents and matching | Existing profile/matching tests exercise extraction and generation; installer tests enter a written profile, review RSS selection and Flash/Pro settings. Invalid Flash JSON is reported and logged, with no activation or partial publication. |
| Save/resume and cancel | Active configuration stays absent/unchanged; Resume reuses the generated proposal without more model calls; credentials are absent from terminal review. |
| Reinstall Keep | No generation calls; configuration/history retained; selected timers only. |
| Disable/re-enable an input | Installer preserves queued work/history. RSS process test pauses collection and triage, makes no provider requests, then resumes and delivers the retained job; audit reports the pause. |
| Disabled workers | Real worker entry points return without provider requests. |
| Optional features | Weekly reporting without email intake; researched briefings plus canary; browser/search probes, real PDF generation and selected timers. |
| Verification/activation failures | Fresh delivery failure never activates; reinstall input failure restores running timers; partial activation restores files and removes newly enabled timers. |
| Credentials on reinstall | Token refresh between proposal creation and Start is retained; refresh during verification survives configuration rollback for the same account. |
| Stale proposals | Active changes made after Save are not overwritten. |
| Existing service files | Old daily RSS units migrate; customized generated units survive; contradictory schedule overrides produce an explicit error. |
| Real systemd | Unique temporary units verify enable/start, actual harmless worker execution, feature changes, restoration of existing timers, and cleanup of a fresh installation. Paths include spaces, `%` and `$`. |

Full regression: **377 tests run, 372 passed, five skipped** in a disposable copy with
loopback networking permitted. The real-systemd test is opt-in and was run
separately. `git diff --check` also passed.

Installer process tests replace external provider transports, Docker and systemd;
they do not replace wizard, validation, generation or activation logic. The
real-systemd test uses harmless workers and never touches production unit names.
Existing RSS end-to-end tests use loopback feeds, model responses and delivery
endpoints, exercising real collector/triage/audit processes.

Live DeepSeek, Telegram and Microsoft account authorization was **not** repeated
for this change. Real Docker installation/browser download and a pristine OS
package installation were **not** repeated. Start performs the configured input
and delivery checks on the user's actual installation. First real job processing
and weekly email delivery remain observable runtime outcomes, not installer
success claims. This is not a crash-atomic installer: power loss/forced termination
can interrupt multi-file activation.

To reproduce, first make a disposable checkout/copy as described in
[CLAUDE.md](../CLAUDE.md), install requirements and seed `state/channels.json`
from `channels.json.example`. Never run the full suite against personal state:

```sh
jobsmcp/bin/python3 -m unittest discover -s tests -t . -p 'test_*.py' -q
```

Focused installer checks create their own temporary installations:

```sh
jobsmcp/bin/python3 -m unittest tests.test_system_setup tests.test_setup_services -v
```

On a host with a user systemd manager, this opt-in test creates and removes only
units named `tg-setup-test-<random>-*`:

```sh
RUN_SYSTEMD_SETUP_TESTS=1 jobsmcp/bin/python3 -m unittest tests.test_setup_services.RealSystemd -v
```
