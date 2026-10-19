# RSS feature verification

The [feed selection plan](rss-feed-plan.md) is implemented and verified against its
acceptance criteria. No production setup, timers or live deliveries were activated.

## Evidence

- **Regression:** 359 tests run in a disposable repository copy; 355 passed, four
  skipped, no failures (63.637 seconds). The skips require private generated
  `interests.txt`, `skills.txt`, `tech_stack.txt` and criteria files, deliberately
  absent from the copy. Dedicated setup tests generate these in temporary state.
- **Public check:** all 36 catalogue subscriptions parsed, four FSF/NoDesk detail
  pages extracted, no errors. Checked 2026-10-18 at 20:10 UTC; endpoint URLs,
  counts, dates, hashes and comparisons are in [the report](rss-feed-verification.json).
  The check uses no models, delivery services or runtime state.
- **Health command:** `audit --health` exited successfully in the disposable copy.
  That copy has no live ingestion history; the behavioral tests below establish
  lifecycle checks, including deliberately broken histories.
- **Patch hygiene:** `git diff --check` passed.

| Requirement | Implementation and behavioral evidence |
| --- | --- |
| Publisher capabilities | [Catalogue](../lib/rss_catalog.json): FOSSJobs 12 choices, FSF 1, JobHub 1, WWR 12, NoDesk 9, Tails 1. [Publisher tests](../tests/test_rss_publishers.py) exercise all six formats, valid-empty Tails, FSF information/volunteer exclusions, NoDesk entities, WWR regional variants and detail extraction. Fixtures are synthetic, sanitized representations of observed structures. |
| Identity across subscriptions | [Subscription tests](../tests/test_rss_subscriptions.py) cover legacy migration, overlap, differing descriptions, later changes, replay after queue consumption and an interrupted checkpoint write. Existing publisher/job/message identities survive; a newly selected feed representation does not resend a known job. |
| Reviewed setup | [Selection tests](../tests/test_rss_selection.py) cover several professions, current preferences, bounded JSON IDs, one correction attempt, manual fallback, no RSS, Keep without requests, custom/disabled sources, Revise and Later. Failed preview or later Flash generation publishes nothing. [Installer tests](../tests/test_system_setup.py) exercise first install and reinstall through `setup.sh`. |
| Summary enrichment | FSF and NoDesk fetch full details after local exclusions. Cached by feed revision; at most 20 detail requests per publisher per poll. Subscription and process tests prove failed work remains pending after leaving the feed window and reaches triage on recovery. |
| Backfill and polling | Subscription tests cover recent/future/all behavior, unknown dates, explicit backfill expansion, baseline migration, per-subscription cadence, changed configuration and isolated source failures. Setup previews counts/dates before Apply. The hourly timer uses `--due`; quiet catalogue sources remain daily. |
| Pipeline and audit boundary | Seven [process tests](../tests/test_rss_e2e.py) run real collector, triage and audit entry points against loopback feed/model/delivery services. They assert model calls and delivery acknowledgements, replay suppression, migration and outage recovery. [Audit tests](../tests/test_rss_audit.py) and subscription tests detect missing/malformed evidence, unaccounted or stalled arrivals, failed decisions, missing/duplicate acknowledgements and overdue subscriptions. Audit only reads recorded evidence. |
| Direction and prompts | [Matching setup tests](../tests/test_matching_setup.py) retain Flash/Pro generation and direction propagation coverage. RSS selection receives the full current preferences; changing direction alone does not silently change subscriptions. |

## Publisher findings

The public check confirmed that native categories affect coverage, not just volume:
category feeds contain jobs absent from broad feeds. WWR “All Programming” omitted
three back-end jobs. Both tested JobHub keyword/category query parameters left
membership unchanged, so only its general feed is offered. FSF and NoDesk detail
pages supplied substantially more text than their summaries. Tails was valid and
empty. Catalogue entries retain official discovery links and coverage caveats;
unverified geography/skill filters are not selectable.

## Reproduce

Run the full suite **in a disposable copy**, using the repository virtual environment:

```bash
jobsmcp/bin/python3 -m unittest discover -s tests -t . -p 'test_*.py' -v
jobsmcp/bin/python3 audit --health
```

Process tests require loopback sockets. For an isolated public recheck, choose a
fresh output directory; this downloads public feeds and detail pages:

```bash
jobsmcp/bin/python3 scripts/verify_rss_catalog.py --output /tmp/rss-verification
```

## Limits

These checks verify the feature's behavior, not live model recommendation quality
or live delivery. Public results are a dated observation. Publisher windows and
outages can lose unseen jobs; old listings need not remain open. Page-only changes
are detected only when the feed entry changes. Deduplication is within a publisher;
cross-site matching, automatic history pruning, adaptive polling and ongoing
coverage optimization remain outside this feature. Audit does not choose feeds,
judge relevance, call networks/models or repair state.
