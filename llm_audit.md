# LLM Audit Log

Every LLM decision — including errors — is appended as a JSON line to one of two audit files:

| Pipeline | File |
|---|---|
| Telegram groups | `~/.it-jobs/it-jobs-triage.jsonl` |
| Email | `~/.it-jobs/email-triage.jsonl` |

Operational events (token refresh, cursor updates, queue errors) are **not** in the audit log — they go to `~/.it-jobs/it-jobs.log` via systemd.

---

## Record schemas

### `it_jobs_cyprus` — batch Flash

```json
{
  "ts": "2026-05-10T14:23:01+00:00",
  "channel": "it_jobs_cyprus",
  "message_id": "18423",
  "stage": "flash",
  "model": "deepseek-v4-flash",
  "decision": "flag",
  "is_vacancy": true,
  "reason": "Python backend role at a fintech startup"
}
```

`is_vacancy` distinguishes CVs/off-topic posts from actual job offers. `decision` is `flag`, `skip`, or `error`.

---

### `cyithr` — line-by-line Flash (one record per line read)

```json
{
  "ts": "2026-05-10T14:23:05+00:00",
  "channel": "cyithr",
  "message_id": "9901",
  "stage": "flash",
  "model": "deepseek-v4-flash",
  "line": 2,
  "of": 11,
  "line_text": "🐍 Python / ML Engineer — Limassol",
  "decision": "relevant",
  "reason": "Python + ML role in Cyprus matches profile"
}
```

`line` / `of` show how deep into the post Flash read before deciding. `line_text` is the line that triggered the decision. `decision` is `irrelevant`, `relevant`, `next`, or `error`.

---

### Pro evaluation (both Telegram channels)

```json
{
  "ts": "2026-05-10T14:23:18+00:00",
  "channel": "cyithr",
  "message_id": "9901",
  "stage": "pro",
  "model": "deepseek-v4-pro",
  "decision": "send",
  "reason": "Strong Python/ML match, Limassol onsite with remote option, competitive salary"
}
```

`decision` is `send`, `skip`, or `error`.

---

### Email — Flash (header only)

```json
{
  "ts": "2026-05-10T10:07:12+00:00",
  "channel": "email",
  "stage": "flash",
  "model": "deepseek-v4-flash",
  "from": "jobs@mlcompany.com",
  "subject": "Research Engineer — NeurIPS 2026 deadline",
  "decision": "read",
  "reason": "Industry ML role with conference deadline, check body"
}
```

`decision` is `read`, `skip`, or `error`.

---

### Email — Pro (full body)

```json
{
  "ts": "2026-05-10T10:07:31+00:00",
  "channel": "email",
  "stage": "pro",
  "model": "deepseek-v4-pro",
  "from": "jobs@mlcompany.com",
  "subject": "Research Engineer — NeurIPS 2026 deadline",
  "decision": "send",
  "reason": "PyTorch/probabilistic ML role, remote-friendly, NeurIPS deadline in 3 weeks"
}
```

---

## `decision` values

| Value | Meaning |
|---|---|
| `flag` | Flash passed to Pro (it_jobs_cyprus) |
| `skip` | LLM decided not relevant |
| `irrelevant` | Flash stopped reading (cyithr line-by-line) |
| `relevant` | Flash escalated to Pro (cyithr line-by-line) |
| `next` | Flash requested next line (cyithr — intermediate steps) |
| `read` | Flash requested body fetch (email) |
| `send` | Pro decided to deliver to Telegram |
| `error` | LLM call or JSON parse failed — not a model decision |

---

## Useful `jq` queries

```bash
# All decisions for a specific message
jq 'select(.message_id == "9901")' ~/.it-jobs/it-jobs-triage.jsonl

# How many lines did cyithr messages take before a decision?
jq 'select(.channel == "cyithr" and .decision != "next") | {message_id, line, of, decision}' \
  ~/.it-jobs/it-jobs-triage.jsonl

# All errors across both pipelines
jq 'select(.decision == "error")' ~/.it-jobs/it-jobs-triage.jsonl
jq 'select(.decision == "error")' ~/.it-jobs/email-triage.jsonl

# Everything sent to Telegram today
jq 'select(.decision == "send" and (.ts | startswith("2026-05-10")))' \
  ~/.it-jobs/it-jobs-triage.jsonl ~/.it-jobs/email-triage.jsonl

# Flash skip rate per channel
jq -s 'group_by(.channel) | map({channel: .[0].channel, total: length, skipped: map(select(.decision == "skip" or .decision == "irrelevant")) | length})' \
  ~/.it-jobs/it-jobs-triage.jsonl
```
