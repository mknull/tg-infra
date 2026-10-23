# Add a website from a GitHub issue

Start here when recommending a job website. The workflow investigates the issue
and comments, asks for human acceptance, proposes a source addition, and verifies
real extraction → Flash → Pro → test delivery before opening a PR.

This is **development tooling**, separate from the standalone jobs application.
It needs `git`, authenticated `gh` with issue/PR write access, a logged-in Claude
Code runner supporting `--restricted`, and the existing `jobsmcp` environment.
Development uses the runner's paid account. Live verification additionally needs
a DeepSeek API key and Telegram bot with access to a **dedicated test chat**.
The normal installation, its state, prompts, and timers are never changed.

## 1. Investigate

From a clean, committed checkout containing this workflow:

```bash
jobsmcp/bin/python3 scripts/website_issue.py assess \
  https://github.com/OWNER/REPO/issues/123 \
  --site examplejobs --run state/website-issues/123
```

Use the repository containing that issue; the driver clones this checkout and
later publishes its branch to the issue's repository. The agent reads the issue,
comments and actual website, then reports policy evidence, observed job volume,
quality, native filters, extraction feasibility, and inaccuracies in the issue.
Policy-prohibited use is rejected. Unclear permission waits for resolution.
The issue receives the assessment, its hash, and the next required human action.

The run directory is private and contains a separate checkout, transcripts,
proposed changes, approvals, commit checks and evaluation evidence. Never commit
this directory. Choose a new run directory for a fresh assessment.

## 2. Approve the assessment and evaluation contract

Review `assessment.json`. Independently choose actual postings covering relevant,
irrelevant and borderline cases. Do not select only the jobs the proposed feed
or extractor happened to find. Define a bounded inventory: dates, categories,
listing pages or other sampling method. Metrics describe that sample, not the
entire website or every possible user.

Create an approval JSON file outside the agent checkout, for example:

```json
{
  "approve": true,
  "assessment_sha256": "HASH_PRINTED_IN_THE_ISSUE",
  "setup_files": [],
  "evaluation": {
    "inventory": "Human-reviewed postings from the site's listings on DATE, including category-filter exclusions",
    "choices": ["examplejobs:all"],
    "min_precision": 0.9,
    "min_recall": 0.95,
    "profile": {
      "desired_roles": "Paid Python backend roles open to Cyprus residents",
      "acceptable_roles": "Related paid software engineering roles",
      "criteria": "Send paid Python backend roles available remotely from Cyprus. Reject unpaid or US-only positions.",
      "direction_full": "Also consider backend infrastructure roles.",
      "direction_small": "Include backend infrastructure roles."
    },
    "cases": [
      {"url": "https://example.org/jobs/1", "relevant": true,
       "reason": "Paid Python role, worldwide remote", "reference_text": "ACTUAL COMPLETE POSTING TEXT"},
      {"url": "https://example.org/jobs/2", "relevant": false,
       "reason": "US residents only", "reference_text": "ACTUAL COMPLETE POSTING TEXT"}
    ]
  }
}
```

Replace all examples with reviewed evidence; two cases illustrate the format,
not an adequate universal sample size. The limit is 60 cases to bound live cost.
Use a publishable evaluation profile and posting text you are permitted to store:
the driver retains this reference in the source addition's tests. Do not put API
keys, a private CV, or restricted posting text in the approval file.

The profile supplies the exact reviewed Flash roles, Pro criteria and full/small
preference deltas used by this evaluation. This is not a test of generating a
profile from a CV; existing setup tests cover that separate operation.

Prefer `setup_files: []`: catalogue entries already appear in manual selection
and the recommendation catalogue. If necessary, explicitly approve individual
files from `lib/rss_selection.py`, `lib/setup_matching.py`, `lib/setup_wizard.py`.
Those changes must serve only this source; a human still reviews their meaning.

## 3. Develop one checked commit

```bash
jobsmcp/bin/python3 scripts/website_issue.py develop state/website-issues/123 \
  --approval /path/to/approval.json
```

The agent has only read/search/web tools and returns file contents as data.
It cannot execute commands, edit files, commit, update GitHub, or access the
evaluator's credentials. The driver protects the approved reference labels and validates the entire
proposal, constructs the new catalogue entry without modifying existing ones,
and checks the proposed tree before creating a commit. It checks every commit
again on subsequent development, verification and acceptance.

Permitted additions are the website's own tests, fixtures and documentation,
its catalogue entry, and specifically approved setup files. Current ingestion
supports RSS/Atom with existing parsing options and two detail-page extractors.
A website requiring a new shared parser, dependency, scheduler, or triage change
is rejected under this boundary. Broader support requires a separate task.
The shared [extractor contract](#extractor-contract) and ingestion code are also
protected from changes by individual website issues.

A forbidden proposal or tool attempt is a rejection, even if later reverted.
`proposal.json` records file content and stated rationale; agent transcripts
record observable tool activity. This is not access to private internal reasoning.
The [runner's restricted tool surface](https://code.claude.com/docs/en/cli-reference)
provides the read-only agent boundary. Git checking is owned by the driver, not a
bypassable hook inside the agent's checkout.

The driver does not execute newly generated Python before a human reviews it.
Review the patch and tests. A failed verification can be followed by another
`develop` invocation with the **same** approval to produce another checked commit.
Each invocation has a default development budget of $5 (`assess`: $3), adjustable
with `--budget`; these are runner budgets, not DeepSeek evaluation budgets.

## Extractor contract

The current RSS/Atom integrations use [shared ingestion](../lib/job_ingestion.py).
Extraction returns a list of ordinary job dictionaries:

| Field | Contract |
| --- | --- |
| `source_id` | Nonempty string: the website's stable posting ID, unique within the extraction. |
| `url` | HTTP(S) posting URL. |
| `title`, `content` | Nonempty strings; preserve the job text needed for triage. |
| `posted_at` | Optional timezone-aware ISO timestamp; unknown dates remain `null`. |
| `categories` | Optional list of nonempty strings; defaults to `[]`. |

Shared ingestion validates the whole extraction before queueing and computes
`content_hash` from the exact content. Empty results require the existing explicit
`allow_empty` setting. Required detail extraction uses the same job contract and
must preserve `source_id` and `url`; failures retain the original job for retry.
Structural validation does not establish that the text is complete or relevant.

The shared path owns IDs, revisions, deduplication, queueing, poll status, observed
job dispositions and arrival records. Existing `rss-*` IDs, `state/rss/` files and
`audit/rss.jsonl` records remain compatible with triage and audit. No migration is
needed. Audit checks collection → processing → delivery; policy, relevance and
unseen-job coverage remain the human-gated evaluation's responsibility.

Supported websites remain RSS/Atom plus the existing FSF/NoDesk detail parsers.
No extractor registry or additional website types are introduced. Website
verification runs the contract, subscription and audit regressions before paid
live evaluation; the per-issue edit allowlist is unchanged.

## 4. Approve and run live verification

```bash
jobsmcp/bin/python3 scripts/website_issue.py verify state/website-issues/123 \
  --approve-live --commit EXACT_COMMIT_FROM_THE_ISSUE \
  --env-file /path/to/private.env --test-chat YOUR_TEST_CHAT_ID
```

The private env file needs `DEEPSEEK_API_KEY` and `TELEGRAM_BOT_TOKEN`.
This command explicitly authorizes execution of the reviewed code/tests, paid
model requests, and messages to the designated test chat. Do not use a normal
recipient's chat. It first runs RSS/setup and website regressions in a disposable
copy without the supplied credentials. Failure stops before paid evaluation.

The live evaluator uses the actual setup selection functions, website endpoints,
poller, queue processor, model API, and delivery. Only labelled jobs are sent to
triage; other extracted inventory is retained separately. Source omissions count
toward the end-to-end matrix. Errors remain errors rather than true negatives.
The actual processor is rerun to check that it does not repeat calls/delivery.
There are at most five model calls per sampled posting; every rerun requires
explicit approval and has a fresh evaluation directory.

Evidence includes TP/FP/TN/FN, precision/recall, the stage responsible for each
disagreement, model reasons, reference and extracted text, exact requests and
responses, source observations, and delivery acknowledgements. Current job triage
has no model-selected tools; the development transcript separately records the
development agent's tools. Existing runtime audit remains an observer of arrival,
processing and delivery; the evaluator owns relevance scoring.

Read the local `report.json`, `requests.jsonl`, `configuration.json`, `tests.txt`
and `state/audit/` in the evaluation directory. Verify that current postings still
mean what their reference labels say. Model replies and live sites can change;
passing evidence is specific to the recorded commit, profile and sample.
Exact prompts stay private; issue updates contain summary counts and evidence
locations. Keys are removed from temporary runtime configuration on completion.

## 5. Accept or reject

Acceptance requires passing thresholds, successful regression tests, no processing
or delivery errors, and evidence for the exact current commit. For disagreements,
provide a human review file mapping **each disagreement URL** to its accepted
limitation and supporting reason. An explanation does not turn an FP/FN into a
correct decision. Fixes require another commit and evaluation. Label corrections
require a fresh human-reviewed run; the developer cannot edit frozen labels.

```bash
jobsmcp/bin/python3 scripts/website_issue.py finish state/website-issues/123 accept \
  --reason 'Reviewed policy, setup changes, current postings and evaluation evidence.' \
  --review /path/to/disagreement-review.json
```

Omit `--review` if there are no disagreements. Acceptance pushes the feature branch,
opens a PR, and updates the issue with **accepted and built** and the PR link.
It does not merge, close the issue, deploy, or modify an installation.

```bash
jobsmcp/bin/python3 scripts/website_issue.py finish state/website-issues/123 reject \
  --reason 'The site prohibits the proposed automated use; policy URL and explanation.'
```

For later live checks, keep the reference JSON and invoke `website_evaluate.py`
with `--approve-live`, `--checkout`, `--reference`, `--output` (a fresh private directory),
`--env-file`, and `--test-chat`. This is an explicit manual live command, never
ordinary CI. Reconfirm policy and review expired/changed postings first.

## Interruptions and boundaries

Invalid agent JSON, unavailable credentials, network failures and timeouts stop
the phase without declaring the website accepted. Inspect local transcripts;
they do not justify automatic extra spending. Pending issue updates are retained:

```bash
jobsmcp/bin/python3 scripts/website_issue.py sync state/website-issues/123
jobsmcp/bin/python3 scripts/website_issue.py check state/website-issues/123
```

`sync` retries comments without restarting development or live evaluation.
`check` inspects every commit, so an unrelated change followed by a revert still
fails. Paths cannot establish the semantic correctness of an allowed setup edit;
human patch review remains necessary. The driver and human approval files are
trusted local tooling, not a sandbox against a malicious local operator.
