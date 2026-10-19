# RSS feed selection plan

Implemented and verified. Baseline: `4a0fe06`. Publisher checks: 2026-10-18.
See [verification results and limits](rss-feed-verification.md) for evidence against
the acceptance criteria below. Production activation is outside this plan.

Goal: choose the feeds that cover the user's interests with the fewest unnecessary
downloads, model calls and repeated notifications. Prefer publisher filtering where
its behavior is verified; keep uncertain matches for Flash/Pro. Scope: the six
current RSS publishers, using the existing queue and delivery pipeline.

Completion means **the feature is verified to work**: reviewed feed choices reach
the existing pipeline, preserve job identity and installation choices, and pass the
behavioral checks below. Keep preference-based recommendations and manual selection
in scope. Stop after those checks; production activation, ongoing tuning and a
broader source-discovery project are separate work.

## What each publisher actually offers

These are point-in-time observations, not guarantees of completeness or uptime.

| Publisher | Verified evidence | Implemented treatment |
| --- | --- | --- |
| [FOSSJobs](https://www.fossjobs.net/rss/) | Publishes all-jobs and 11 category feeds. `/rss/researchers/` and `/rss/programmers/` worked. Each sampled feed contained ten entries; research included entries from 2018. Nine research entries were absent from the all-jobs snapshot. | Offer all published categories, selected from user preferences. Use category feeds to improve coverage as well as reduce irrelevant intake. Preview historical backfill; a category feed is not proof that every listed vacancy remains open. Employment-type, location and search feeds remain unverified. |
| [FSF](https://www.fsf.org/resources/jobs/) | Advertises [a static jobs feed](https://static.fsf.org/fsforg/rss/jobs.xml), byte-identical to the configured legacy endpoint in this check. Six entries: two paid vacancies, a volunteer posting and three information pages. Vacancy descriptions were short summaries. No category feed was discovered. | Keep one jobs subscription. Exclude known information URLs and explicitly volunteer entries when the user wants paid work. Replace the required title prefix with exclusions that allow unfamiliar vacancy titles through. Validate detail-page enrichment for summaries. |
| [Open Source JobHub](https://opensourcejobhub.com/jobs/) | HTML search supports keywords, location and categories, but advertises only [the general feed](https://opensourcejobhub.com/rss/). Adding `categories[]=System Administration` or an impossible `q` keyword returned the same 32 entries and identical bytes. | Keep the general feed unless a documented, behaviorally verified filtered endpoint is found. Do not transfer HTML query parameters to RSS by assumption. Retain full feed descriptions; apply only reliable local metadata filters before Flash. |
| [We Work Remotely](https://weworkremotely.com/remote-job-rss-feed) | Publishes named category feeds. The sampled “All Programming” feed omitted three back-end entries present in both the all-jobs and back-end feeds. | Offer the explicit published categories, including nontechnical roles. Validate category coverage and overlap before treating a combined feed as a substitute. Remove the fixed programming/DevOps preference. Preserve attribution. |
| [NoDesk](https://nodesk.co/remote-jobs/engineering/) | Engineering page advertises [its own RSS feed](https://nodesk.co/remote-jobs/engineering/index.xml), which worked. General and engineering feeds each contained ten summary entries. Sampled Europe and Python pages did not advertise RSS. | Validate advertised feeds for the site's other role categories. Include geography/skill choices only if advertised and verified during that pass. Retain the existing entity repair and check whether selected jobs need full descriptions. Use hourly polling for these short windows. |
| [Tails](https://tails.net/jobs/) | Advertises [one jobs feed](https://tails.net/jobs/index.rss). It was valid and empty; the jobs page also reported no vacancies. | Keep one optional subscription and valid-empty handling. Check its advertised formats for equivalent coverage; do not subscribe to duplicate RSS/Atom representations. No category machinery is needed without evidence of separate feeds. |

## Implementation sequence

1. **Create a verified feed catalogue.** Store publisher IDs, selectable feed IDs,
   exact URLs, coverage descriptions, known overlap, content completeness, empty-feed
   behavior and evidence/date. Make one pass through the six publishers' feed
   directories and relevant category/search pages. Validate advertised options;
   do not keep probing speculative endpoints. Test that proposed filters change
   membership; HTTP 200 alone proves nothing. Unverified options stay unavailable.
   Keep custom URLs supported. This is data around the
   shared collector, with small publisher-specific rules only where necessary.

2. **Support multiple subscriptions per publisher before enabling category feeds.**
   Keep today's `id` as the publisher identity and accept an optional `subscriptions`
   list; normalize legacy `url` configurations to a single subscription. Track HTTP
   validators, raw responses and poll status per subscription, but deduplicate jobs
   across that publisher's subscriptions before queueing. Preserve existing job IDs,
   queued work, seen ledgers and delivery references during migration. Feed membership
   is provenance, not a content revision. Define deterministic handling of overlapping
   feeds with different descriptions so switching feeds cannot repeatedly resend jobs.

3. **Integrate subscription selection into matching setup.** Use the existing profile
   and full direction/preferences to propose catalogue choices alongside Flash and
   Pro settings. Ask: **Recommend from my preferences / Choose myself / Disable RSS**.
   One bounded JSON response selects known IDs with short reasons;
   application code resolves URLs. Validate IDs, duplicates and incompatible choices
   locally, with one correction attempt. No JEV dependency. Persistent model failures
   offer manual selection, Retry or Later; never silently enable all feeds. Show sites,
   categories, coverage caveats and additions/removals before Apply/Revise/Later.
   Selecting no RSS feeds is valid. Reinstall defaults to Keep existing with no model
   calls; preserve custom URLs and disabled choices. Publish accepted subscriptions
   and matching settings together using the existing staged-write/rollback mechanism.
   Explicit direction updates still
   reach Flash/Pro immediately; subscription changes require a separate reviewed
   proposal using the latest direction, rather than changing silently on each poll.

4. **Make intake match each feed's limitations.** Use source-side categories first,
   then reliable local exclusions, then Flash/Pro. Missing categories or uncertain
   geography must not silently eliminate plausible jobs. For FSF/NoDesk, compare
   representative feed entries with linked job pages; add bounded, cached detail
   fetches only where summaries omit information needed for matching, after
   deterministic filtering. Refresh when the feed entry changes; periodic page-only
   change detection is outside this feature. Record that limitation for those feeds.
   A failed required fetch stays retryable and visible; do not mark the item processed.
   Keep the existing full descriptions for sources that already provide them.

5. **Control initial backfill and feed-window loss.** Preview counts/date spans before
   first activation or adding subscriptions. Offer recent entries (default:
   30 days), future arrivals only, or all available entries. Explain unknown dates;
   retain them in recent mode rather than silently discarding them. Future-only mode
   seeds the observed baseline without sending it. Preserve delivery history when
   expanding or narrowing subscriptions. Poll short/high-volume feeds hourly and
   quiet feeds daily, retaining conditional requests and per-host throttling. Use
   fixed per-subscription cadences for this feature. A quiet feed is not a failed
   poll, and disappearance is not closure. Polling cannot guarantee
   coverage beyond the publisher's retained window or during a prolonged outage.

6. **Verify behavior and stop.** Use sanitized publisher fixtures plus the real
   collector → queue → triage → mocked-delivery process tests. Cover each site,
   filter queries that are ignored, valid empty feeds, short windows, summary-fetch
   failures, overlapping categories, conflicting representations, unchanged replay,
   changed jobs and migration without resends. Test setup with several different
   professions, invalid model JSON, manual selection, no RSS, reinstallation and
   direction changes. Run one isolated live-fetch check with models/delivery disabled
   to verify the catalogue endpoints and compare representative category choices
   with broad feeds. Broader subscriptions remain available when narrowing loses
   relevant coverage. Run the repository regression suite in a disposable copy.
   Record results and remaining limitations; no deployment or observation period
   is required to finish this work. Mocked-delivery tests verify pipeline behavior;
   they do not establish live delivery or the quality of model recommendations.

## Audit boundary and acceptance

Producers record subscription/config revision, fetched and parsed counts, and explicit
item dispositions: queued, unchanged, excluded, duplicate or deferred, with reasons.
Audit reads that evidence to reconcile observed items through processing and delivery
acknowledgements, including individual subscription failures and overdue polls relative
to each subscription's cadence. It does not choose feeds,
assess relevance, validate the catalogue by browsing, call models or repair anything.
Missing or malformed evidence must remain distinguishable from an empty source.

Acceptance is demonstrated by:

- Verified catalogue endpoints and publisher fixtures, including ignored filters,
  summaries and valid empty feeds.
- Process tests reaching a mocked delivery acknowledgement, with overlap, replay,
  changed entries, backfill, failure/retry and migration without duplicate sends.
- Setup tests covering reviewed recommendations, manual/no-RSS choices, invalid
  model JSON, reinstallation and direction propagation.
- Runtime evidence accounting for observed items, with audit detecting subscription
  failures and processing/delivery gaps without assessing relevance or choosing feeds.
- No subscription-selection model calls during normal polling, and passing regression
  checks in a disposable copy.

These checks establish observed behavior, not complete website coverage or optimal
relevance. Cost benchmarks, long-term coverage studies and adaptive polling are outside
the completion criteria.

Deliver in three reviewable changes: catalogue/contracts; subscription identity and
collector behavior; setup selection and verification. Keep the current simple subscription
configuration working throughout. Cross-publisher job matching and the other sources
in the imported ingestion brief remain separate work.
